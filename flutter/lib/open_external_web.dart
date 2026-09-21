/// Web build: hand the URL to the browser.
///
/// A new tab (`_blank`), because every link the app opens this way leaves the
/// library — the same rule the credits footer follows on the web client.
library;

import 'package:web/web.dart' as web;

Future<bool> openExternal(String url) async {
  web.window.open(url, '_blank');
  return true;
}
