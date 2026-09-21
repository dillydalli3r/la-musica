import 'dart:async';

import 'package:flutter/material.dart';

import '../api.dart';
import '../models.dart';
import '../shell.dart';
import '../state.dart';
import '../theme.dart';
import '../widgets/shelves.dart';

/// What each job kind is called on screen (the registry's own names). A kind
/// this build does not know is shown as its raw id rather than hidden — a job
/// the page cannot name is still locking files.
const Map<String, String> _kindLabels = {
  'scripts': 'Script run',
  'import': 'Import',
  'organize': 'Organize',
  'tags': 'Tag write',
  'remove': 'Remove from library',
};

/// The library work running right now, and the files it has locked: while a job
/// is listed here, deleting, moving, organizing or retagging any folder it
/// holds is refused by the server instead of racing the job. There is no
/// "force release" — the claim belongs to the work holding it.
class InProgressPage extends StatefulWidget {
  const InProgressPage({super.key});

  @override
  State<InProgressPage> createState() => _InProgressPageState();
}

class _InProgressPageState extends State<InProgressPage> {
  /// The registry forgets a job the moment it ends, so a row left on screen
  /// from an older fetch would read as a job that hung. A poll, not a
  /// subscription.
  static const Duration _interval = Duration(seconds: 5);

  List<JobLock>? _jobs;
  String? _error;
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    // The ApiClient is reached through an inherited widget, which initState may
    // not read; the first frame is where that lookup is legal.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      _load();
      _timer = Timer.periodic(_interval, (_) => _load(quiet: true));
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _load({bool quiet = false}) async {
    if (!mounted) return;
    final client = AppScope.of(context).api;
    if (client == null) return;
    if (!quiet) setState(() => _error = null);
    try {
      final jobs = await client.jobLocks();
      if (mounted) {
        setState(() {
          _jobs = jobs;
          _error = null;
        });
      }
    } on ApiException catch (e) {
      // A poll that fails with jobs already on screen keeps them: the error
      // belongs to the refresh, not to the list.
      if (mounted && (_jobs == null || !quiet)) {
        setState(() => _error = e.message);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null) return ErrorView(message: _error!, onRetry: _load);
    final jobs = _jobs;
    if (jobs == null) return const Center(child: CircularProgressIndicator());
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);

    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        SectionLabel(
          'In progress',
          trailing: Text(
            jobs.isEmpty ? 'idle' : '${jobs.length} running',
            style: const TextStyle(fontSize: 11),
          ),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(4, 0, 4, 8),
          child: Text(
            'Library work running now, and the files it has locked — nothing else can delete, '
            'move or retag those paths until the job finishes.',
            style: TextStyle(fontSize: 12, color: muted),
          ),
        ),
        if (jobs.isEmpty)
          const EmptyHint(
            text: 'Nothing is running — the library is idle.',
            icon: Icons.pending_actions_outlined,
          )
        else
          for (final job in jobs) _JobCard(job: job),
      ],
    );
  }
}

/// One in-flight job: what it is, how long it has been running, how far it has
/// got, and the folders it holds.
class _JobCard extends StatelessWidget {
  const _JobCard({required this.job});

  final JobLock job;

  @override
  Widget build(BuildContext context) {
    final muted = Theme.of(
      context,
    ).colorScheme.onSurface.withValues(alpha: 0.55);
    final accent = Theme.of(context).colorScheme.primary;
    final progress = job.progress;

    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 12, 14, 8),
            child: Row(
              children: [
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 8,
                    vertical: 2,
                  ),
                  decoration: BoxDecoration(
                    color: accent.withValues(alpha: 0.15),
                    border: Border.all(color: accent.withValues(alpha: 0.3)),
                    borderRadius: BorderRadius.circular(4),
                  ),
                  child: Text(
                    _kindLabels[job.kind] ??
                        (job.kind.isEmpty ? 'Job' : job.kind),
                    style: TextStyle(fontSize: 11, color: accent),
                  ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    job.label,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      fontSize: 13,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  'running for ${_runningFor(job)}',
                  style: TextStyle(fontSize: 11, color: muted),
                ),
              ],
            ),
          ),
          if (progress != null)
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 0, 14, 10),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      progress.text ?? 'working',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: 11, color: muted),
                    ),
                  ),
                  if (progress.total != null && progress.total! > 0)
                    Text(
                      '${progress.done?.floor() ?? 0}/${progress.total!.floor()}',
                      style: TextStyle(fontSize: 11, color: muted),
                    ),
                ],
              ),
            ),
          if (job.paths.isEmpty)
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 0, 14, 12),
              child: Row(
                children: [
                  Icon(Icons.lock_open_outlined, size: 14, color: muted),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      'no folder held right now — between steps',
                      style: TextStyle(fontSize: 11, color: muted),
                    ),
                  ),
                ],
              ),
            )
          else
            // An album lock can be dozens of paths, so the list stays folded
            // until it is asked for.
            ExpansionTile(
              dense: true,
              tilePadding: const EdgeInsets.symmetric(horizontal: 14),
              childrenPadding: const EdgeInsets.only(bottom: 6),
              leading: Icon(Icons.lock_outline, size: 16, color: muted),
              title: Text(
                '${job.paths.length} locked folder${job.paths.length == 1 ? '' : 's'}',
                style: const TextStyle(fontSize: 12),
              ),
              children: [
                for (final path in job.paths)
                  ListTile(
                    dense: true,
                    contentPadding: const EdgeInsets.fromLTRB(30, 0, 14, 0),
                    title: Text(
                      path.replaceAll('\\', '/'),
                      style: const TextStyle(
                        fontSize: 12,
                        fontFamily: 'monospace',
                      ),
                    ),
                  ),
              ],
            ),
        ],
      ),
    );
  }

  /// The server measures the run time itself, so a client whose clock is off
  /// still shows the true one; a job that reported no start shows "—".
  static String _runningFor(JobLock job) {
    final elapsed =
        job.elapsed ??
        (job.startedAt == null
            ? null
            : DateTime.now().millisecondsSinceEpoch / 1000 - job.startedAt!);
    if (elapsed == null || elapsed < 0) return '—';
    final total = elapsed.floor();
    if (total < 60) return '${total}s';
    final minutes = total ~/ 60;
    if (minutes < 60) {
      return '${minutes}m ${(total % 60).toString().padLeft(2, '0')}s';
    }
    return '${minutes ~/ 60}h ${(minutes % 60).toString().padLeft(2, '0')}m';
  }
}
