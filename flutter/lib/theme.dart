import 'package:flutter/material.dart';

/// The app's two themes, in the flat "monochrome" language the React UI uses:
/// near-black surfaces with a white accent in dark, paper white with a
/// near-black accent in light. The dark theme is the default — the app is a
/// library browser that people leave open while music plays.
///
/// BLACK & WHITE IS THE DEFAULT, and it is the same default the web client
/// ships (web/src/index.css: `--accent: 255 255 255`). The accent carries no
/// hue at all: in dark it is white with near-black contrast text, in light it
/// is near-black with white contrast text. An earlier build seeded both themes
/// from one indigo (`#6366f1`), which is where the purple rails, pills and
/// cover placeholders came from.
///
/// Colours mirror `web/src/index.css`'s tokens so the two front ends read as
/// one product: `bg` #09090b, `raise` #18181b, `border` #27272a, accent as
/// above, muted text #71717a.
class AppTheme {
  /// Dark: the accent is white, text on it is near-black (the inverse of the
  /// near-black app bar it sits on).
  static const Color accentDark = Color(0xFFFFFFFF);
  static const Color onAccentDark = Color(0xFF09090B);
  /// Light: the accent is near-black, text on it is white.
  static const Color accentLight = Color(0xFF18181B);
  static const Color onAccentLight = Color(0xFFFFFFFF);
  static const Color _bgDark = Color(0xFF09090B);
  static const Color _raiseDark = Color(0xFF18181B);
  static const Color _borderDark = Color(0xFF27272A);

  static ThemeData dark() => _base(
    brightness: Brightness.dark,
    scheme: const ColorScheme.dark(
      surface: _bgDark,
      primary: accentDark,
      onPrimary: onAccentDark,
      surfaceContainerHighest: _raiseDark,
      outline: _borderDark,
    ),
    scaffold: _bgDark,
    card: _raiseDark,
    border: _borderDark,
  );

  static ThemeData light() => _base(
    brightness: Brightness.light,
    scheme: const ColorScheme.light(
      surface: Color(0xFFFAFAFA),
      primary: accentLight,
      onPrimary: onAccentLight,
      surfaceContainerHighest: Colors.white,
      outline: Color(0xFFE4E4E7),
    ),
    scaffold: const Color(0xFFFAFAFA),
    card: Colors.white,
    border: const Color(0xFFE4E4E7),
  );

  static ThemeData _base({
    required Brightness brightness,
    required ColorScheme scheme,
    required Color scaffold,
    required Color card,
    required Color border,
  }) {
    final muted = brightness == Brightness.dark
        ? const Color(0xFF71717A)
        : const Color(0xFF71717A);
    return ThemeData(
      useMaterial3: true,
      brightness: brightness,
      colorScheme: scheme,
      scaffoldBackgroundColor: scaffold,
      canvasColor: scaffold,
      dividerTheme: DividerThemeData(color: border, space: 1, thickness: 1),
      cardTheme: CardThemeData(
        color: card,
        elevation: 0,
        margin: EdgeInsets.zero,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(10),
          side: BorderSide(color: border),
        ),
      ),
      appBarTheme: AppBarTheme(
        backgroundColor: scaffold,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
        centerTitle: false,
        titleTextStyle: TextStyle(
          fontSize: 16,
          fontWeight: FontWeight.w600,
          color: brightness == Brightness.dark ? Colors.white : Colors.black,
        ),
      ),
      navigationRailTheme: NavigationRailThemeData(
        backgroundColor: scaffold,
        indicatorColor: scheme.primary.withValues(alpha: 0.16),
        selectedIconTheme: IconThemeData(color: scheme.primary, size: 20),
        unselectedIconTheme: IconThemeData(color: muted, size: 20),
        selectedLabelTextStyle: TextStyle(color: scheme.primary, fontSize: 11),
        unselectedLabelTextStyle: TextStyle(color: muted, fontSize: 11),
      ),
      textTheme: const TextTheme().apply(
        bodyColor: brightness == Brightness.dark ? Colors.white : Colors.black,
        displayColor: brightness == Brightness.dark
            ? Colors.white
            : Colors.black,
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: card,
        isDense: true,
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 12,
          vertical: 12,
        ),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8),
          borderSide: BorderSide(color: border),
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8),
          borderSide: BorderSide(color: border),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(8),
          borderSide: BorderSide(color: scheme.primary),
        ),
      ),
      filledButtonTheme: FilledButtonThemeData(
        style: FilledButton.styleFrom(
          backgroundColor: scheme.primary,
          foregroundColor: Colors.white,
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        ),
      ),
      outlinedButtonTheme: OutlinedButtonThemeData(
        style: OutlinedButton.styleFrom(
          foregroundColor: brightness == Brightness.dark
              ? Colors.white
              : Colors.black,
          side: BorderSide(color: border),
          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(8)),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
        ),
      ),
      snackBarTheme: SnackBarThemeData(
        behavior: SnackBarBehavior.floating,
        backgroundColor: brightness == Brightness.dark
            ? _raiseDark
            : Colors.black87,
      ),
    );
  }
}

/// A shelf/list heading: small, uppercase, muted — the same label the React
/// UI draws above every group of rows.
class SectionLabel extends StatelessWidget {
  const SectionLabel(this.text, {super.key, this.trailing});

  final String text;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final style = TextStyle(
      fontSize: 11,
      letterSpacing: 1.6,
      fontWeight: FontWeight.w600,
      color: Theme.of(context).colorScheme.onSurface.withValues(alpha: 0.45),
    );
    return Padding(
      padding: const EdgeInsets.fromLTRB(4, 18, 4, 8),
      child: Row(
        children: [
          Text(text.toUpperCase(), style: style),
          const Spacer(),
          if (trailing != null) trailing!,
        ],
      ),
    );
  }
}
