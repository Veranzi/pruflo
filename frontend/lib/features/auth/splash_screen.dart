import 'dart:async';

import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../core/auth_service.dart';
import '../../widgets/brand_logo.dart';

/// Brand splash shown on launch, then hands off to sign-in or the dashboard.
class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key, this.from});

  /// The page that was open before a reload; reopened once signed in.
  final String? from;

  @override
  State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen> {
  final _auth = AuthService.instance;
  Timer? _minimum;
  bool _shownLongEnough = false;

  @override
  void initState() {
    super.initState();
    // Show the brand briefly, and longer only while the session is checked.
    _minimum = Timer(const Duration(milliseconds: 2000), () {
      _shownLongEnough = true;
      _maybeContinue();
    });
    _auth.addListener(_maybeContinue);
  }

  void _maybeContinue() {
    if (!mounted || !_shownLongEnough || !_auth.ready) return;
    _auth.removeListener(_maybeContinue);
    final from = widget.from;
    // Only app routes: never follow an absolute URL from the query string.
    final back = from != null && from.startsWith('/') && !from.startsWith('//')
        ? from
        : '/dashboard';
    context.go(_auth.isSignedIn ? back : '/login');
  }

  @override
  void dispose() {
    _minimum?.cancel();
    _auth.removeListener(_maybeContinue);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Container(
        decoration: const BoxDecoration(
          gradient: LinearGradient(
            begin: Alignment.topCenter,
            end: Alignment.bottomCenter,
            colors: [AppColors.navy, Color(0xFF0E2A66)],
          ),
        ),
        child: Stack(
          children: [
            // Soft wave in the lower part of the screen, as in the mockup.
            Positioned.fill(
              child: CustomPaint(painter: _WavePainter()),
            ),
            Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Image.asset(PrufloMark.asset, width: 120),
                  const SizedBox(height: 16),
                  const Text(
                    'Pruflo',
                    style: TextStyle(
                      fontSize: 36,
                      fontWeight: FontWeight.w700,
                      color: Colors.white,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    'Verify. Validate. Build Trust.',
                    style: TextStyle(
                      fontSize: 15,
                      color: Colors.white.withValues(alpha: 0.8),
                    ),
                  ),
                ],
              ),
            ),
            Positioned(
              left: 0,
              right: 0,
              bottom: 56,
              child: Center(
                child: SizedBox(
                  width: 48,
                  child: ClipRRect(
                    borderRadius: BorderRadius.circular(4),
                    child: LinearProgressIndicator(
                      minHeight: 3,
                      color: const Color(0xFF4F86FF),
                      backgroundColor: Colors.white.withValues(alpha: 0.15),
                    ),
                  ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _WavePainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final h = size.height, w = size.width;
    final back = Path()
      ..moveTo(0, h * 0.78)
      ..quadraticBezierTo(w * 0.35, h * 0.68, w * 0.6, h * 0.76)
      ..quadraticBezierTo(w * 0.85, h * 0.84, w, h * 0.72)
      ..lineTo(w, h)
      ..lineTo(0, h)
      ..close();
    final front = Path()
      ..moveTo(0, h * 0.86)
      ..quadraticBezierTo(w * 0.4, h * 0.78, w * 0.7, h * 0.86)
      ..quadraticBezierTo(w * 0.9, h * 0.91, w, h * 0.84)
      ..lineTo(w, h)
      ..lineTo(0, h)
      ..close();
    canvas.drawPath(
        back, Paint()..color = const Color(0xFF1652F0).withValues(alpha: 0.18));
    canvas.drawPath(
        front, Paint()..color = const Color(0xFF4F86FF).withValues(alpha: 0.12));
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
