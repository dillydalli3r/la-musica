import 'package:flutter/material.dart';

import '../models.dart';
import '../state.dart';
import '../theme.dart';

/// Bytes in the units a person reads, or "unknown" when the OS would not say
/// — never a zero the app invented.
String fmtBytes(int? bytes) {
  if (bytes == null) return 'unknown';
  if (bytes < 1024) return '$bytes B';
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  var value = bytes / 1024;
  var unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit++;
  }
  return '${value.toStringAsFixed(value >= 100 ? 0 : 1)} ${units[unit]}';
}

/// How much room the library takes, against what the volume has left: the same
/// figures the React client's Storage card shows, from the same endpoint.
///
/// A volume whose size cannot be read (a network mount) renders "unknown"
/// rather than a bar at 0%, and the app's own folders are listed with what they
/// hold so a user can see where the space went.
class StorageCard extends StatefulWidget {
  const StorageCard({super.key});

  @override
  State<StorageCard> createState() => _StorageCardState();
}

class _StorageCardState extends State<StorageCard> {
  StorageInfo? _info;
  String? _error;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _load());
  }

  Future<void> _load() async {
    final client = AppScope.of(context).api;
    if (client == null) return;
    try {
      final info = await client.storage();
      if (mounted) setState(() => _info = info);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    }
  }

  @override
  Widget build(BuildContext context) {
    final info = _info;
    if (_error != null) {
      return Padding(
        padding: const EdgeInsets.only(top: 8),
        child: Text(
          'Storage unavailable — $_error',
          style: const TextStyle(fontSize: 12, color: Color(0xFFF87171)),
        ),
      );
    }
    if (info == null) {
      return const Padding(
        padding: EdgeInsets.only(top: 12),
        child: SizedBox(
          height: 18,
          width: 18,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      );
    }
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final pct = info.percentUsed;
    final barColor = pct == null
        ? muted
        : pct >= 90
        ? const Color(0xFFEF4444)
        : pct >= 75
        ? const Color(0xFFF59E0B)
        : const Color(0xFF10B981);

    Widget row(String label, String value) => Padding(
      padding: const EdgeInsets.symmetric(vertical: 2),
      child: Row(
        children: [
          Expanded(
            child: Text(label, style: TextStyle(fontSize: 12, color: muted)),
          ),
          Text(value, style: const TextStyle(fontSize: 12)),
        ],
      ),
    );

    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SectionLabel(
            'Storage',
            trailing: Text(
              info.mount ?? info.label ?? '',
              style: TextStyle(fontSize: 11, color: muted),
            ),
          ),
          ClipRRect(
            borderRadius: BorderRadius.circular(3),
            child: LinearProgressIndicator(
              value: pct == null ? 0 : (pct / 100).clamp(0.0, 1.0),
              minHeight: 6,
              backgroundColor: Theme.of(
                context,
              ).colorScheme.surfaceContainerHighest,
              valueColor: AlwaysStoppedAnimation<Color>(barColor),
            ),
          ),
          const SizedBox(height: 6),
          Text(
            pct == null
                ? 'Volume size unknown'
                : '${pct.toStringAsFixed(1)}% used · ${fmtBytes(info.freeBytes)} free of ${fmtBytes(info.totalBytes)}',
            style: TextStyle(fontSize: 11, color: muted),
          ),
          const SizedBox(height: 4),
          if (info.library != null)
            row(
              'Library (${info.library!.files} files)',
              fmtBytes(info.library!.bytes),
            ),
          if (info.appData != null)
            row('App data', fmtBytes(info.appData!.bytes)),
          if (info.trash != null) row('Trash', fmtBytes(info.trash!.bytes)),
          if (info.downloadBytes != null)
            row(
              info.downloadStagingBytes != null &&
                      info.downloadStagingBytes! > 0
                  ? 'Downloads (${fmtBytes(info.downloadStagingBytes)} staging)'
                  : 'Downloads',
              fmtBytes(info.downloadBytes),
            ),
          if (info.skippedCount > 0)
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Text(
                '${info.skippedCount} folder(s) could not be read',
                style: const TextStyle(fontSize: 11, color: Color(0xFFF59E0B)),
              ),
            ),
        ],
      ),
    );
  }
}
