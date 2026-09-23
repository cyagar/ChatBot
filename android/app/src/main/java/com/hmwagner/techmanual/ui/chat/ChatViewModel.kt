package com.hmwagner.techmanual.ui.chat

import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.hmwagner.techmanual.network.ApiClient
import com.hmwagner.techmanual.network.CitationOut
import com.hmwagner.techmanual.network.ConversationOut
import com.hmwagner.techmanual.network.EvidenceOut
import com.hmwagner.techmanual.network.FeedbackRequest
import com.hmwagner.techmanual.network.MessageIn
import com.hmwagner.techmanual.network.MessageOut
import com.hmwagner.techmanual.network.SetMachineRequest
import java.util.UUID
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

/**
 * A local-only echo of the technician's own typed question. The server never
 * returns the user's message from POST .../messages (only the assistant
 * reply) -- see routes_chat.py's ask_question -- so this is how the app shows
 * "what I just asked" before/while the answer arrives. It's overwritten by
 * the real persisted messages on the next full reload (retry, clarify
 * resume, screen re-entry), so it never drifts from the server's record.
 */
data class LocalEcho(val id: String, val content: String)

data class ChatUiState(
    val conversation: ConversationOut? = null,
    val messages: List<MessageOut> = emptyList(),
    val pendingEcho: LocalEcho? = null,
    // True when a send failed with a network exception, or a reload after a
    // 409 found no trace of the question at all (server never even
    // persisted it) -- genuinely don't know whether the server got it (see
    // the comment in send()'s catch block, and loadMessages()). Lets the UI
    // tell "still in flight" apart from "we lost track of this one".
    val pendingEchoUncertain: Boolean = false,
    // True when a reload after a 409 confirms the OPPOSITE of uncertain: the
    // server definitely has this exact question persisted (its own last
    // message is that user turn) and is still working on it, or died before
    // finishing. Deliberately a separate flag from pendingEchoUncertain --
    // "definitely accepted, still generating" and "no idea if this was even
    // received" are different situations and need different copy (P0A-2;
    // collapsing them into one "uncertain" label showed "Connection lost"
    // over a question the server had already accepted).
    val pendingEchoStillProcessing: Boolean = false,
    val composerText: String = "",
    val sending: Boolean = false,
    val loadingHistory: Boolean = true,
    val error: String? = null,
    val evidence: EvidenceOut? = null,
    val evidenceDocumentId: Int? = null,
    val evidenceLoading: Boolean = false,
    // P0A-4: non-2xx and exceptions used to leave both evidence and this
    // null, so the sheet (gated on evidenceLoading || evidence != null)
    // never even opened -- the request silently failed with no visible
    // error and no way to retry. evidenceCitation is retained so Retry can
    // redrive the exact same request without the caller re-supplying it.
    val evidenceError: String? = null,
    val evidenceCitation: CitationOut? = null,
    val savedMessageIds: Set<Int> = emptySet(),
    // P1-02 (external review, 2026-09-21): each rated message's own rating,
    // keyed by message id -- mirrors savedMessageIds so a reload shows
    // "already rated" instead of resetting the buttons to blank.
    val feedbackByMessageId: Map<Int, String> = emptyMap(),
)

class ChatViewModel(private val conversationId: Int) : ViewModel() {
    private val _state = MutableStateFlow(ChatUiState())
    val state: StateFlow<ChatUiState> = _state

    // Negative, decrementing, and never reused for the lifetime of this
    // ViewModel -- guaranteed not to collide with a real server-assigned
    // message id (always a positive DB primary key), which matters because
    // LazyColumn keys list items on MessageOut.id (see ChatScreen).
    private var nextLocalMessageId = -1

    // P0-07 fix, take 2 (external review, 2026-09-21): the general `sending`
    // flag is not precise enough to guard `messages` in loadMessages() below
    // -- selectClarifyingMachine() also sets sending=true and then calls
    // loadMessages() itself as its OWN update mechanism (not a concurrent
    // unrelated refresh), and guarding on `sending` there blocked that
    // legitimate path from ever seeing its reload's messages (broke
    // "confirming a clarifying machine keeps the composer locked..."). This
    // flag is scoped specifically to performSend()'s own askQuestion() call
    // being in flight, which is the one and only scenario an UNRELATED
    // refresh() (e.g. pull-to-refresh) can race against.
    private var askQuestionInFlight = false

    init {
        refresh()
    }

    fun refresh() {
        viewModelScope.launch { loadMessages() }
    }

    // Split out from refresh() so selectClarifyingMachine() can await a reload
    // in the same coroutine instead of firing a detached one via refresh() --
    // launching a child coroutine and returning immediately let its own
    // `finally` clear `sending` before the reload actually finished, which
    // re-enabled the composer while the conversation was still mid-reload.
    private suspend fun loadMessages() {
        _state.value = _state.value.copy(loadingHistory = true, error = null)
        try {
            // P1-13 (external review, 2026-09-21): fetched every reload, not
            // just after selectClarifyingMachine() -- otherwise state.conversation
            // stays null (and the toolbar falls back to the stale label
            // passed through navigation) whenever the server resolves a
            // machine for this conversation through any other path (a
            // mention in the question). Best-effort: a failure here must
            // never block the messages themselves from loading.
            val convResp = try {
                ApiClient.service.getConversation(conversationId)
            } catch (e: CancellationException) {
                throw e
            } catch (_: Exception) {
                null
            }
            if (convResp?.isSuccessful == true) {
                _state.value = _state.value.copy(conversation = convResp.body())
            }

            val msgs = ApiClient.service.getMessages(conversationId)
            if (msgs.isSuccessful) {
                val loaded = msgs.body().orEmpty()
                val current = _state.value
                val pending = current.pendingEcho
                // A send() genuinely in flight (its own performSend()
                // coroutine hasn't returned yet) owns pendingEcho/its status
                // exclusively -- ChatScreen's PullToRefreshBox has no guard
                // against pulling to refresh while sending is true, and a
                // reload landing in that window must not race the original
                // send: clearing pendingEcho out from under it, or showing a
                // false "still waiting" banner over a question that hasn't
                // even had a chance to fail yet (P0A-2).
                val sendInFlight = current.sending
                // A pending question is only actually answered once the
                // reload's own last message is the assistant's reply -- this
                // used to unconditionally clear pendingEcho on ANY successful
                // GET, which silently dropped the "still processing"
                // affordance the moment a duplicate-Idempotency-Key 409 (see
                // performSend) reloaded a conversation whose last persisted
                // row was still just the user's own turn (the answer never
                // finished generating, or the server crashed mid-attempt),
                // or even a reload that found nothing persisted at all yet
                // (the original POST may never have reached the server).
                // Either way, the question must stay visibly pending and
                // recoverable, not look complete or vanish.
                val answered = loaded.isNotEmpty() && loaded.last().role == "assistant"
                val stillUnanswered = pending != null && !sendInFlight && !answered
                // The two ways a question can be "still unanswered" need
                // different, accurate copy, not one collapsed "uncertain"
                // label: a 409 reload whose last message IS this user's turn
                // means the server definitely accepted it and is still
                // working -- "Connection lost, unknown if received" would be
                // simply wrong there. Only an empty/unrelated reload is a
                // genuine "no idea if this was even received".
                val acceptedAndProcessing = stillUnanswered && loaded.isNotEmpty() && loaded.last().role == "user"
                // When accepted-and-processing, drop that trailing persisted
                // user turn from the visible list rather than also keeping
                // pendingEcho -- both would otherwise render the exact same
                // question twice (once as a normal message bubble, once as
                // the pending one).
                val displayMessages = if (acceptedAndProcessing) loaded.dropLast(1) else loaded
                // Rehydrate saved state from the server's own record, not
                // just the messages list itself -- a freshly (re)created
                // ChatViewModel (rotation between panes, an app restart, or
                // simply leaving and re-entering this conversation)
                // previously started this map empty every time, so the
                // button reset to unmarked and a re-tap silently duplicated
                // the saved_answers row server-side (found via live tablet
                // testing 2026-08-25).
                _state.value = current.copy(
                    // External review P0-07 (2026-09-21): this assignment used
                    // to run unconditionally, even while performSend()'s own
                    // POST was still awaiting its response -- so a
                    // pull-to-refresh landing in that window could load the
                    // server's already-persisted user+assistant turns into
                    // `messages` here, and when the original POST's response
                    // then arrived, performSend's success handler
                    // unconditionally appended its own synthetic user turn +
                    // the same (already-present) assistant answer on top --
                    // the server-assigned answer id ended up in `messages`
                    // twice, which LazyColumn(items, key={it.id}) treats as a
                    // duplicate-key error. Deliberately guarded on the
                    // narrower askQuestionInFlight, not the general
                    // `sendInFlight`/`current.sending` used below --
                    // selectClarifyingMachine() also sets `sending=true` and
                    // then calls loadMessages() itself as its OWN update
                    // mechanism (not a concurrent unrelated refresh), so
                    // guarding this on `sending` broke that path entirely
                    // (first attempt at this fix; caught by the existing
                    // "confirming a clarifying machine..." test).
                    // askQuestionInFlight is true only while performSend()'s
                    // own network call is in flight, which is the one
                    // scenario an unrelated refresh() can actually race.
                    messages = if (askQuestionInFlight) current.messages else displayMessages,
                    loadingHistory = false,
                    pendingEcho = if (sendInFlight) current.pendingEcho else if (stillUnanswered) pending else null,
                    pendingEchoUncertain = if (sendInFlight) current.pendingEchoUncertain else stillUnanswered && !acceptedAndProcessing,
                    pendingEchoStillProcessing = if (sendInFlight) current.pendingEchoStillProcessing else acceptedAndProcessing,
                    error = if (sendInFlight) {
                        current.error
                    } else if (acceptedAndProcessing) {
                        "That question was already sent and is still being answered. Tap Retry to check again."
                    } else if (stillUnanswered) {
                        "Still waiting to hear back on that question. Pull to refresh or tap Retry to check again."
                    } else null,
                    savedMessageIds = loaded.filter { it.is_saved }.map { it.id }.toSet(),
                    feedbackByMessageId = loaded.mapNotNull { m -> m.feedback_rating?.let { m.id to it } }.toMap(),
                )
            } else {
                _state.value = _state.value.copy(loadingHistory = false, error = "Couldn't load this conversation (code ${msgs.code()}).")
            }
        } catch (_: Exception) {
            _state.value = _state.value.copy(loadingHistory = false, error = "Can't reach the server. Check your connection.")
        }
    }

    fun onComposerChange(v: String) {
        _state.value = _state.value.copy(composerText = v)
    }

    fun send() {
        val text = _state.value.composerText.trim()
        if (text.isEmpty() || _state.value.sending) return

        val echo = LocalEcho(UUID.randomUUID().toString(), text)
        _state.value = _state.value.copy(
            sending = true,
            error = null,
            composerText = "",
            pendingEcho = echo,
            pendingEchoUncertain = false,
            pendingEchoStillProcessing = false,
        )
        viewModelScope.launch { performSend(echo) }
    }

    /**
     * Resends a still-pending question from an "uncertain" bubble (connection
     * lost mid-send, or the server says it's already processing) using the
     * SAME idempotency key and text as the original attempt -- never a
     * freshly generated key, and never whatever's currently typed in the
     * composer. Reusing the key is what makes this safe to tap more than
     * once: if the original attempt actually landed, the server replays its
     * result instead of creating a second user turn (routes_chat.py's
     * ask_question).
     */
    fun retryPendingSend() {
        val echo = _state.value.pendingEcho ?: return
        if (_state.value.sending) return
        _state.value = _state.value.copy(
            sending = true,
            error = null,
            pendingEchoUncertain = false,
            pendingEchoStillProcessing = false,
        )
        viewModelScope.launch { performSend(echo) }
    }

    private suspend fun performSend(echo: LocalEcho) {
        try {
            // echo.id doubles as the Idempotency-Key -- it's already a UUID
            // generated once per composed question and held steady across
            // retries (see LocalEcho/retryPendingSend), which is exactly what
            // the key needs to be. askQuestionInFlight brackets ONLY this
            // call (finally clears it before any of the branches below run,
            // including the 409 branch's own loadMessages() call), so a
            // concurrent refresh() is only ever blocked from touching
            // `messages` for the actual duration this response is pending.
            val resp = try {
                askQuestionInFlight = true
                ApiClient.service.askQuestion(conversationId, MessageIn(echo.content), idempotencyKey = echo.id)
            } finally {
                askQuestionInFlight = false
            }
            when {
                resp.isSuccessful -> {
                    val answer = resp.body()!!
                    // The pendingEcho bubble was rendered AFTER the message
                    // list (see ChatScreen), so once the answer landed in
                    // `messages` while pendingEcho stayed set, the user's own
                    // question kept rendering below the assistant's reply --
                    // backwards conversation order. Fold the echoed question
                    // into `messages` as a synthetic local turn (the server
                    // never returns the user's own message from this
                    // endpoint -- see LocalEcho's doc comment) in the correct
                    // position, then clear pendingEcho so it isn't rendered twice.
                    val userTurn = MessageOut(
                        id = nextLocalMessageId--,
                        role = "user",
                        content = echo.content,
                        created_at = "",
                    )
                    _state.value = _state.value.copy(
                        sending = false,
                        messages = _state.value.messages + userTurn + answer,
                        pendingEcho = null,
                        pendingEchoUncertain = false,
                        pendingEchoStillProcessing = false,
                    )
                }
                resp.code() == 409 -> {
                    // The same idempotency key was already accepted -- the
                    // original attempt is still generating (or, rarely, the
                    // server died mid-attempt). Either way, starting a fresh
                    // attempt with a NEW key would be exactly the
                    // duplicate-question hazard idempotency keys exist to
                    // prevent, so keep the same pending echo/key and check
                    // whether it's actually finished now. No point setting an
                    // "already sent, checking…" message of our own here --
                    // loadMessages() (P0A-2) immediately resets `error` on
                    // entry anyway and owns the real "still unanswered"
                    // status/copy once it knows whether the reload actually
                    // found a reply.
                    _state.value = _state.value.copy(sending = false)
                    loadMessages()
                }
                else -> {
                    // External review P0-06 (2026-09-21): this used to restore
                    // echo.content into the composer unconditionally -- but
                    // composerText was cleared to "" only at the START of this
                    // same send() call; if the technician typed a NEW question
                    // while this one was still failing server-side, that draft
                    // is what's sitting in composerText right now, and
                    // restoring the OLD failed question over it silently threw
                    // the newer draft away. Only restore when the composer is
                    // still empty (nothing newer has been typed); otherwise
                    // leave the newer draft alone and adjust the message
                    // accordingly, since "your draft wasn't lost" would be
                    // false in that case.
                    val newerDraftTyped = _state.value.composerText.isNotEmpty()
                    _state.value = _state.value.copy(
                        sending = false,
                        pendingEcho = null,
                        pendingEchoUncertain = false,
                        pendingEchoStillProcessing = false,
                        error = if (newerDraftTyped) {
                            "Couldn't send that question (code ${resp.code()})."
                        } else {
                            "Couldn't send that question (code ${resp.code()}). Your draft wasn't lost -- retype it."
                        },
                        composerText = if (newerDraftTyped) _state.value.composerText else echo.content,
                    )
                }
            }
        } catch (_: Exception) {
            // We don't know whether the server actually received this --
            // retryOnConnectionFailure is disabled specifically so we never
            // silently resend it. Surface the honest ambiguity rather than
            // guessing. pendingEcho is deliberately left set (not cleared) so
            // the question doesn't just vanish, and its key is reused if the
            // technician taps Retry -- so even a blind retry here can't
            // create a duplicate turn.
            _state.value = _state.value.copy(
                sending = false,
                pendingEchoUncertain = true,
                pendingEchoStillProcessing = false,
                error = "Lost connection while sending. Tap Retry to check whether it went through.",
            )
        }
    }

    fun selectClarifyingMachine(machineId: Int) {
        viewModelScope.launch {
            _state.value = _state.value.copy(sending = true, error = null)
            try {
                val resp = ApiClient.service.setConversationMachine(conversationId, SetMachineRequest(machineId))
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(conversation = resp.body())
                    // Awaited directly (not refresh(), which launches a
                    // detached child coroutine and returns immediately) so
                    // `finally` below can't clear `sending` -- re-enabling the
                    // composer -- before the reload it's supposed to be
                    // gating has actually finished.
                    loadMessages()
                } else {
                    _state.value = _state.value.copy(error = "Couldn't set the machine (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Can't reach the server. Check your connection.")
            } finally {
                _state.value = _state.value.copy(sending = false)
            }
        }
    }

    fun retry(messageId: Int) {
        viewModelScope.launch {
            _state.value = _state.value.copy(sending = true, error = null)
            try {
                val resp = ApiClient.service.retryAnswer(conversationId, messageId)
                if (resp.isSuccessful) {
                    val updated = resp.body()!!
                    _state.value = _state.value.copy(
                        sending = false,
                        messages = _state.value.messages.map { if (it.id == updated.id) updated else it },
                    )
                } else {
                    _state.value = _state.value.copy(sending = false, error = "Retry failed (code ${resp.code()}).")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(sending = false, error = "Can't reach the server. Check your connection.")
            }
        }
    }

    fun saveAnswer(messageId: Int) {
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.saveAnswer(messageId)
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(savedMessageIds = _state.value.savedMessageIds + messageId, error = null)
                } else {
                    _state.value = _state.value.copy(error = "Couldn't save that answer (code ${resp.code()}). Try again.")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Couldn't save that answer -- check your connection and try again.")
            }
        }
    }

    // P1-21 (external review, 2026-09-21): the Save button was permanently
    // disabled once tapped, with no way to undo it from the chat screen.
    fun unsaveAnswer(messageId: Int) {
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.unsaveAnswer(messageId)
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(savedMessageIds = _state.value.savedMessageIds - messageId, error = null)
                } else {
                    _state.value = _state.value.copy(error = "Couldn't remove that saved answer (code ${resp.code()}). Try again.")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Couldn't remove that saved answer -- check your connection and try again.")
            }
        }
    }

    fun submitFeedback(messageId: Int, rating: String, comment: String? = null) {
        viewModelScope.launch {
            try {
                val resp = ApiClient.service.submitFeedback(messageId, FeedbackRequest(rating, comment))
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(
                        feedbackByMessageId = _state.value.feedbackByMessageId + (messageId to rating),
                        error = null,
                    )
                } else {
                    _state.value = _state.value.copy(error = "Couldn't record that feedback (code ${resp.code()}). Try again.")
                }
            } catch (_: Exception) {
                _state.value = _state.value.copy(error = "Couldn't record that feedback -- check your connection and try again.")
            }
        }
    }

    // External review P0-08 (2026-09-21): each citation tap used to start an
    // uncancelled coroutine, with no cancellation on dismiss and no check
    // that a response still belonged to the citation currently open. Tapping
    // citation B while A was still loading let A's response land last and
    // overwrite B's evidence/page under B's still-displayed citation header
    // -- a direct safety risk, since technicians use citations to verify
    // manual instructions. Fixed with the two guards below: cancelling the
    // previous load's Job before starting a new one (covers both a new tap
    // and dismissal), and comparing a monotonically increasing request token
    // before applying any success/error, so even a response that slips past
    // cancellation (already in flight when cancel() was called) is ignored
    // if it's not for the request that's still current.
    private var evidenceJob: kotlinx.coroutines.Job? = null
    private var evidenceRequestToken = 0

    fun openCitation(citation: CitationOut) {
        _state.value = _state.value.copy(
            evidenceCitation = citation,
            evidenceLoading = true,
            evidence = null,
            evidenceError = null,
            evidenceDocumentId = citation.document_id,
        )
        loadEvidence(citation)
    }

    /** Redrives the same request as the last [openCitation] call, without losing the citation context. */
    fun retryEvidence() {
        val citation = _state.value.evidenceCitation ?: return
        _state.value = _state.value.copy(evidenceLoading = true, evidence = null, evidenceError = null)
        loadEvidence(citation)
    }

    private fun loadEvidence(citation: CitationOut) {
        evidenceJob?.cancel()
        val token = ++evidenceRequestToken
        evidenceJob = viewModelScope.launch {
            try {
                val resp = ApiClient.service.getEvidence(citation.document_id, citation.chunk_id)
                if (token != evidenceRequestToken) return@launch
                if (resp.isSuccessful) {
                    _state.value = _state.value.copy(evidenceLoading = false, evidence = resp.body())
                } else {
                    _state.value = _state.value.copy(
                        evidenceLoading = false,
                        evidenceError = "Couldn't load this evidence (code ${resp.code()}).",
                    )
                }
            } catch (e: kotlinx.coroutines.CancellationException) {
                // A newer citation tap (or dismiss) cancelled THIS job via
                // evidenceJob?.cancel() above -- that's expected, cooperative
                // cancellation, not a failure. Rethrowing (rather than
                // falling into the generic catch below, which used to treat
                // this identically to a real network error and apply a
                // stale error state on top of whatever the newer
                // request/dismiss had already set) is required for
                // structured concurrency regardless.
                throw e
            } catch (_: Exception) {
                if (token != evidenceRequestToken) return@launch
                _state.value = _state.value.copy(
                    evidenceLoading = false,
                    evidenceError = "Can't reach the server. Check your connection.",
                )
            }
        }
    }

    fun dismissEvidence() {
        evidenceJob?.cancel()
        evidenceRequestToken++
        _state.value = _state.value.copy(
            evidence = null,
            evidenceDocumentId = null,
            evidenceError = null,
            evidenceCitation = null,
            // Also reset here (pre-existing gap, not previously reachable
            // without the token/cancellation fix above): dismissing while a
            // load was still in flight used to leave evidenceLoading=true
            // forever, since only openCitation/loadEvidence ever set it.
            evidenceLoading = false,
        )
    }

    class Factory(private val conversationId: Int) : ViewModelProvider.Factory {
        @Suppress("UNCHECKED_CAST")
        override fun <T : androidx.lifecycle.ViewModel> create(modelClass: Class<T>): T {
            return ChatViewModel(conversationId) as T
        }
    }
}
