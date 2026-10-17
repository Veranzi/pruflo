import 'package:firebase_core/firebase_core.dart';

/// Build-time configuration, passed with `--dart-define=NAME=value`.
///
/// Nothing here is secret: Firebase web config values are public by design
/// (access is enforced by Firebase Auth and the API, not by hiding them).
class AppConfig {
  AppConfig._();

  /// The Cloud Run API, e.g. https://pruflo-api-xxxx.a.run.app
  static const apiBaseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://localhost:8000',
  );

  static const _firebaseApiKey = String.fromEnvironment('FIREBASE_API_KEY');
  static const _firebaseAppId = String.fromEnvironment('FIREBASE_APP_ID');
  static const _firebaseSenderId =
      String.fromEnvironment('FIREBASE_MESSAGING_SENDER_ID');
  static const _firebaseProjectId = String.fromEnvironment('FIREBASE_PROJECT_ID');
  // Defaults to the project's own <projectId>.firebaseapp.com.
  static const _firebaseAuthDomain = String.fromEnvironment('FIREBASE_AUTH_DOMAIN');

  /// reCAPTCHA v3 site key for Firebase App Check. When set, every API call
  /// carries an App Check token (X-Firebase-AppCheck), which the API requires
  /// in production unless PRUFLO_APP_CHECK_REQUIRED=false.
  static const appCheckSiteKey =
      String.fromEnvironment('FIREBASE_APP_CHECK_SITE_KEY');

  /// Web push VAPID key (Firebase console → Cloud Messaging → Web Push
  /// certificates). When set together with the Firebase config, the app asks
  /// for notification permission after sign-in and registers the device for
  /// push (POST /v1/devices). Empty: no push, in-app notifications only.
  static const vapidKey = String.fromEnvironment('FIREBASE_VAPID_KEY');

  /// Without a Firebase API key the app uses development sign-in, which the
  /// API accepts only when it runs with PRUFLO_ENV=local.
  static bool get firebaseConfigured =>
      _firebaseApiKey.isNotEmpty && _firebaseProjectId.isNotEmpty;

  static bool get pushConfigured => firebaseConfigured && vapidKey.isNotEmpty;

  static FirebaseOptions get firebaseOptions => FirebaseOptions(
        apiKey: _firebaseApiKey,
        appId: _firebaseAppId,
        messagingSenderId: _firebaseSenderId,
        projectId: _firebaseProjectId,
        authDomain: _firebaseAuthDomain.isNotEmpty
            ? _firebaseAuthDomain
            : '$_firebaseProjectId.firebaseapp.com',
      );
}
