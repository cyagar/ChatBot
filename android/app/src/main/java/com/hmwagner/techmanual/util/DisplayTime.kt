package com.hmwagner.techmanual.util

import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle
import java.util.Locale

private val DISPLAY_FORMAT: DateTimeFormatter =
    DateTimeFormatter.ofLocalizedDateTime(FormatStyle.MEDIUM, FormatStyle.SHORT)

/**
 * Renders a backend ISO-8601 timestamp (UTC, with offset) in the device's
 * time zone and locale. Falls back to the raw text's date-and-minute prefix
 * if it isn't parseable, so a malformed value degrades to something readable
 * rather than crashing the list.
 */
fun formatLocalTimestamp(
    iso: String,
    zone: ZoneId = ZoneId.systemDefault(),
    locale: Locale = Locale.getDefault(),
): String = try {
    OffsetDateTime.parse(iso).atZoneSameInstant(zone).format(DISPLAY_FORMAT.withLocale(locale))
} catch (_: Exception) {
    iso.take(16)
}
