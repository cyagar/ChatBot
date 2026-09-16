package com.hmwagner.techmanual.network

import retrofit2.Response
import retrofit2.http.Body
import retrofit2.http.GET
import retrofit2.http.Header
import retrofit2.http.POST
import retrofit2.http.Path
import retrofit2.http.Query

interface ApiService {

    @POST("api/auth/login")
    suspend fun login(@Body body: LoginRequest): Response<UserOut>

    @POST("api/auth/logout")
    suspend fun logout(): Response<Unit>

    @GET("api/auth/me")
    suspend fun me(): Response<UserOut>

    @GET("api/machines")
    suspend fun searchMachines(@Query("q") query: String = "", @Query("limit") limit: Int = 25): Response<List<MachineOut>>

    @GET("api/machines/recent")
    suspend fun recentMachines(@Query("limit") limit: Int = 10): Response<List<MachineOut>>

    @POST("api/machines/{machineId}/touch")
    suspend fun touchMachine(@Path("machineId") machineId: Int): Response<Unit>

    @POST("api/machines/{machineId}/favorite")
    suspend fun setFavorite(
        @Path("machineId") machineId: Int,
        @Query("favorite") favorite: Boolean,
    ): Response<Unit>

    @POST("api/conversations")
    suspend fun createConversation(@Body body: CreateConversationRequest): Response<ConversationOut>

    @GET("api/conversations")
    suspend fun listConversations(@Query("limit") limit: Int = 20): Response<List<ConversationOut>>

    @GET("api/conversations/{conversationId}/messages")
    suspend fun getMessages(@Path("conversationId") conversationId: Int): Response<List<MessageOut>>

    @POST("api/conversations/{conversationId}/messages")
    suspend fun askQuestion(
        @Path("conversationId") conversationId: Int,
        @Body body: MessageIn,
        // Lets a retry of the same question (after an ambiguous dropped
        // connection) return the original attempt's result instead of
        // creating a second user turn -- see routes_chat.py's ask_question.
        @Header("Idempotency-Key") idempotencyKey: String,
    ): Response<MessageOut>

    @POST("api/conversations/{conversationId}/machine")
    suspend fun setConversationMachine(
        @Path("conversationId") conversationId: Int,
        @Body body: SetMachineRequest,
    ): Response<ConversationOut>

    @POST("api/conversations/{conversationId}/messages/{messageId}/retry")
    suspend fun retryAnswer(
        @Path("conversationId") conversationId: Int,
        @Path("messageId") messageId: Int,
    ): Response<MessageOut>

    @POST("api/messages/{messageId}/save")
    suspend fun saveAnswer(@Path("messageId") messageId: Int): Response<Unit>

    @GET("api/saved-answers")
    suspend fun listSavedAnswers(@Query("limit") limit: Int = 50): Response<List<SavedAnswerOut>>

    @GET("api/manuals/{documentId}/chunks/{chunkId}/evidence")
    suspend fun getEvidence(
        @Path("documentId") documentId: Int,
        @Path("chunkId") chunkId: Int,
    ): Response<EvidenceOut>
}
