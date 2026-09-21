import 'package:flutter/material.dart';

import '../api.dart';
import '../state.dart';

/// First run of a client shell: where is the server?
///
/// The address is PROVEN with a health check before it is saved — a typo has
/// to fail here, with the server's own error, not on the first page the user
/// opens. The web build is served BY the server, so it skips this page
/// entirely (`AppState.connected` is already true for a same-origin client).
class ConnectPage extends StatefulWidget {
  const ConnectPage({super.key, this.onConnected});

  final VoidCallback? onConnected;

  @override
  State<ConnectPage> createState() => _ConnectPageState();
}

class _ConnectPageState extends State<ConnectPage> {
  late final TextEditingController _url = TextEditingController(
    text: AppScope.of(context).serverUrl,
  );
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _url.dispose();
    super.dispose();
  }

  Future<void> _connect() async {
    final state = AppScope.of(context);
    final url = _url.text.trim();
    if (url.isEmpty) {
      setState(() => _error = 'Enter the address of a la musica server');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await state.connect(url);
      widget.onConnected?.call();
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
        constraints: const BoxConstraints(maxWidth: 420),
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                'la musica',
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 26, fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 8),
              Text(
                'This client reads a library from a la musica server. Point it at the '
                'container: 127.0.0.1:8000 on this machine, or the LAN/Tailscale address '
                'of the machine that runs it.',
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
                controller: _url,
                autocorrect: false,
                decoration: const InputDecoration(labelText: 'Server address'),
                onSubmitted: (_) => _connect(),
              ),
              const SizedBox(height: 12),
              FilledButton(
                onPressed: _busy ? null : _connect,
                child: _busy
                    ? const SizedBox(
                        height: 16,
                        width: 16,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('Connect'),
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
