/// Where the project lives, for every surface that points back at it (the
/// rail's GitHub button, the Settings → Project section).
///
/// The server publishes both URLs in `/api/health` (server/version.py derives
/// them from its single REPO constant), and `ProjectLinks.of` prefers that
/// answer — a moved repository then moves every client with it. These
/// constants are the offline fallback for an install old enough to answer
/// without them.
library;

import 'api.dart';

const String projectRepo = 'https://github.com/dillydalli3r/la-musica';
const String projectIssues = '$projectRepo/issues';

/// Both links, from the server when it can say and from the constants when it
/// cannot. One request, no caching layer: the shell asks for health anyway.
class ProjectLinks {
  const ProjectLinks({this.repo = projectRepo, this.issues = projectIssues});

  final String repo;
  final String issues;

  /// From a `/api/health` body — the Settings page already asks for health, so
  /// it reads the links off the same answer instead of a second request. A
  /// server that predates these fields (or answers with something that is not
  /// a URL) falls back to the constants above.
  factory ProjectLinks.fromHealth(Map<String, dynamic> body) {
    final repo = (body['project_url'] as String?) ?? '';
    if (!repo.startsWith('http')) return const ProjectLinks();
    final issues = (body['issues_url'] as String?) ?? '';
    return ProjectLinks(
      repo: repo,
      issues: issues.startsWith('http') ? issues : '$repo/issues',
    );
  }

  static Future<ProjectLinks> load(ApiClient api) async {
    try {
      return ProjectLinks.fromHealth(await api.health());
    } on Exception {
      // An older server, or no server answer at all: the constants are right
      // for this build either way.
      return const ProjectLinks();
    }
  }
}
