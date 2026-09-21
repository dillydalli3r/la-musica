/// Open a URL in the user's own browser.
///
/// Conditional import: the web build opens a tab, everything else shells out
/// to the platform opener. Callers get one function and no `kIsWeb` branch.
library;

export 'open_external_io.dart'
    if (dart.library.js_interop) 'open_external_web.dart';
