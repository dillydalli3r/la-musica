import 'package:flutter/material.dart';

import '../api.dart';
import '../state.dart';

/// Sign in with the server's password. The server's own message is shown
/// verbatim (wrong password, rate limited, …) instead of a generic failure.
class LoginPage extends StatefulWidget {
  const LoginPage({super.key, this.error, this.onSignedIn});

  final String? error;
  final VoidCallback? onSignedIn;

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final TextEditingController _password = TextEditingController();
  final TextEditingController _username = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _password.dispose();
    _username.dispose();
    super.dispose();
  }

  Future<void> _login() async {
    final state = AppScope.of(context);
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await state.login(_password.text, username: _username.text.trim());
      widget.onSignedIn?.call();
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } catch (e) {
      setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    body: Center(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 400),
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Icon(Icons.lock_outline, size: 30),
              const SizedBox(height: 12),
              const Text(
                'Sign in',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 6),
              Text(
                AppScope.of(context).serverUrl,
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontSize: 12,
                  color: Theme.of(
                    context,
                  ).colorScheme.onSurface.withValues(alpha: 0.6),
                ),
              ),
              const SizedBox(height: 20),
              TextField(
                controller: _username,
                decoration: const InputDecoration(labelText: 'User (optional)'),
              ),
              const SizedBox(height: 10),
              TextField(
                controller: _password,
                obscureText: true,
                autofocus: true,
                decoration: const InputDecoration(labelText: 'Password'),
                onSubmitted: (_) => _login(),
              ),
              const SizedBox(height: 14),
              FilledButton(
                onPressed: _busy ? null : _login,
                child: _busy
                    ? const SizedBox(
                        height: 16,
                        width: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('Sign in'),
              ),
              if ((_error ?? widget.error) != null) ...[
                const SizedBox(height: 12),
                Text(
                  (_error ?? widget.error)!,
                  style: const TextStyle(
                    color: Color(0xFFF87171),
                    fontSize: 12,
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    ),
  );
}

/// First run against a server that has no password yet: set one. This is the
/// same call the React wizard makes (`POST /api/auth/setup`), and the server's
/// validation message is shown as-is.
class SetupPasswordPage extends StatefulWidget {
  const SetupPasswordPage({super.key, this.onDone});

  final VoidCallback? onDone;

  @override
  State<SetupPasswordPage> createState() => _SetupPasswordPageState();
}

class _SetupPasswordPageState extends State<SetupPasswordPage> {
  final TextEditingController _password = TextEditingController();
  final TextEditingController _confirm = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _password.dispose();
    _confirm.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final state = AppScope.of(context);
    if (_password.text != _confirm.text) {
      setState(() => _error = 'The two passwords do not match');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await state.setupPassword(_password.text);
      widget.onDone?.call();
    } on ApiException catch (e) {
      setState(() => _error = e.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    body: Center(
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 400),
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Icon(Icons.shield_outlined, size: 30),
              const SizedBox(height: 12),
              const Text(
                'Set a password',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 20, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 6),
              Text(
                'This server has no password yet, so anything on this machine can read '
                'your library. Set one to reach it from your other devices.',
                textAlign: TextAlign.center,
                style: TextStyle(
                  fontSize: 12,
                  color: Theme.of(
                    context,
                  ).colorScheme.onSurface.withValues(alpha: 0.6),
                ),
              ),
              const SizedBox(height: 20),
              TextField(
                controller: _password,
                obscureText: true,
                autofocus: true,
                decoration: const InputDecoration(labelText: 'Password'),
              ),
              const SizedBox(height: 10),
              TextField(
                controller: _confirm,
                obscureText: true,
                decoration: const InputDecoration(labelText: 'Repeat password'),
                onSubmitted: (_) => _submit(),
              ),
              const SizedBox(height: 14),
              FilledButton(
                onPressed: _busy ? null : _submit,
                child: _busy
                    ? const SizedBox(
                        height: 16,
                        width: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('Set password and continue'),
              ),
              if (_error != null) ...[
                const SizedBox(height: 12),
                Text(
                  _error!,
                  style: const TextStyle(
                    color: Color(0xFFF87171),
                    fontSize: 12,
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    ),
  );
}
