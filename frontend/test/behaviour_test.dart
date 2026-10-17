import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:pruflo/app/router.dart';
import 'package:pruflo/core/auth_service.dart';
import 'package:pruflo/main.dart';
import 'package:pruflo/widgets/case_tile.dart';

import 'support/fake_api.dart';

Future<void> _open(WidgetTester tester, String route,
    {Size size = const Size(1440, 900)}) async {
  tester.view.physicalSize = size;
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(const PrufloApp());
  appRouter.go(route);
  await _settle(tester);
}

/// Let the fake API answer; no pumpAndSettle because spinners animate.
Future<void> _settle(WidgetTester tester) async {
  for (var i = 0; i < 8; i++) {
    await tester.pump(const Duration(milliseconds: 100));
  }
}

Future<FakeApi> _signedIn(WidgetTester tester, FakeApi api) async {
  await tester.runAsync(() async {
    await api.install();
    await AuthService.instance.signIn('tester@example.com', '');
  });
  return api;
}

void main() {
  testWidgets('the signed-in user comes from /v1/me', (tester) async {
    await _signedIn(tester, FakeApi(role: 'reviewer'));
    await _open(tester, '/dashboard');
    expect(find.text('Test Reviewer'), findsOneWidget); // top bar
    expect(find.textContaining(', Test'), findsOneWidget); // greeting
    expect(AuthService.instance.user!.role.name, 'reviewer');
    // Reviewers aren't admins: no Admin entry in the sidebar.
    expect(find.text('Admin'), findsNothing);
  });

  testWidgets('dashboard and cases show API data', (tester) async {
    await _signedIn(tester, FakeApi());
    await _open(tester, '/dashboard');
    expect(find.byType(CaseTile), findsNWidgets(2));
    expect(find.text('Case #T-001'), findsWidgets); // case tile and activity
    expect(find.text('Uploaded order.pdf'), findsOneWidget);

    appRouter.go('/cases');
    await _settle(tester);
    expect(find.text('Test case T-001'), findsOneWidget);
  });

  testWidgets('an empty organisation shows empty states, not sample data',
      (tester) async {
    await _signedIn(tester, FakeApi(empty: true));
    await _open(tester, '/dashboard');
    expect(find.text('No cases yet'), findsOneWidget);
    expect(find.text('No activity yet'), findsOneWidget);
    expect(find.byType(CaseTile), findsNothing);

    appRouter.go('/cases');
    await _settle(tester);
    expect(find.text('No cases yet'), findsOneWidget);

    appRouter.go('/integrations');
    await _settle(tester);
    expect(find.text('No API keys yet.'), findsOneWidget);
  });

  testWidgets('an uninvited account sees the API message on the login screen',
      (tester) async {
    final api = FakeApi();
    api.overrides['GET /v1/me'] = (_) => FakeApi.error(
        403, 'not_invited', "This account hasn't been invited to Pruflo.");
    await tester.runAsync(api.install);
    await _open(tester, '/login');

    // Development sign-in: just the email.
    await tester.enterText(find.byType(TextFormField).first, 'stranger@example.com');
    await tester.tap(find.text('Sign In'));
    await tester.runAsync(() => Future<void>.delayed(Duration.zero));
    await _settle(tester);

    expect(find.text("This account hasn't been invited to Pruflo."),
        findsOneWidget);
    expect(AuthService.instance.isSignedIn, isFalse);
  });

  testWidgets('reject needs a note, then the decision is sent', (tester) async {
    final api = await _signedIn(tester, FakeApi(role: 'reviewer'));
    await _open(tester, '/review?case=case_1');

    // The pending finding is selected and shown with its evidence.
    expect(find.text('Quantity shortfall'), findsWidgets);
    expect(find.text('"Qty ordered: 100"'), findsOneWidget);

    await tester.tap(find.text('Reject'));
    await tester.pump();
    await tester.tap(find.text('Save Decision'));
    await tester.pump();
    expect(find.text('Add a note explaining this decision first.'),
        findsOneWidget);
    expect(api.sent('POST', '/decision'), isEmpty);

    await tester.enterText(
        find.widgetWithText(TextField, 'Why? (required)'), 'Wrong supplier');
    await tester.tap(find.text('Save Decision'));
    await _settle(tester);

    final sent = api.sent('POST', '/findings/fnd_1/decision');
    expect(sent, hasLength(1));
    expect(jsonDecode(sent.single.body),
        {'decision': 'reject', 'note': 'Wrong supplier'});
    expect(find.text('Rejected'), findsWidgets);
  });

  testWidgets('viewers cannot decide or export', (tester) async {
    await _signedIn(tester, FakeApi(role: 'viewer'));
    await _open(tester, '/review?case=case_1');
    expect(find.text('Only reviewers and admins can record decisions.'),
        findsOneWidget);
    expect(find.text('Save Decision'), findsNothing);

    appRouter.go('/reports');
    await _settle(tester);
    expect(find.text('Viewers can read reports but not generate them.'),
        findsOneWidget);
    expect(find.text('Export Findings Data'), findsNothing);
  });

  testWidgets('reports can be generated as Word and previewed as PDF',
      (tester) async {
    final api = await _signedIn(tester, FakeApi(role: 'reviewer'));
    await _open(tester, '/reports');

    // The ready PDF report can be previewed and downloaded.
    expect(find.byTooltip('Preview'), findsOneWidget);
    expect(find.byTooltip('Download PDF'), findsOneWidget);

    // The button says which format it makes.
    expect(find.text('Generate PDF Report'), findsOneWidget);
    await tester.tap(find.text('Word'));
    await tester.pump();
    expect(find.text('Generate Word Report'), findsOneWidget);
    await tester.tap(find.text('Generate Word Report'));
    await _settle(tester);

    final sent = api.sent('POST', '/reports');
    expect(sent, hasLength(1));
    expect(jsonDecode(sent.single.body)['format'], 'docx');
  });

  testWidgets('document sources are shown as coming soon', (tester) async {
    await _signedIn(tester, FakeApi());
    await _open(tester, '/integrations');
    await tester.tap(find.text('Sources'));
    await _settle(tester);
    expect(find.text('Coming soon'), findsNWidgets(5));
    expect(find.text('Connected'), findsNothing);
    expect(find.text('Connect'), findsNothing);
  });

  testWidgets('admin system tab shows the region from the API',
      (tester) async {
    await _signedIn(tester, FakeApi());
    await _open(tester, '/admin');
    await tester.tap(find.text('System'));
    await _settle(tester);
    expect(find.text('Singapore (asia-southeast1)'), findsOneWidget);
  });
}
