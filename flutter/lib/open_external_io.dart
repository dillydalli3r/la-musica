/// Desktop build: the platform's own opener.
///
/// `dart:io` + the three commands every desktop already has, rather than a
/// plugin: the app needs exactly one URL opened, and a dependency that ships
/// platform channels for that is heavier than the four lines below. Windows
/// needs the empty `""` title argument — `start` treats a quoted first
/// argument as the window title, so `start "https://…"` opens a blank console
/// instead of the browser.
///
/// Returns false where no opener exists (mobile, an unsupported platform) so
/// the caller can show the URL instead of pretending it opened.
library;

import 'dart:io';

Future<bool> openExternal(String url) async {
  try {
    if (Platform.isWindows) {
      await Process.run('cmd', ['/c', 'start', '', url]);
      return true;
    }
    if (Platform.isMacOS) {
      await Process.run('open', [url]);
      return true;
    }
    if (Platform.isLinux) {
      await Process.run('xdg-open', [url]);
      return true;
    }
  } on ProcessException {
    return false;
  }
  return false;
}
