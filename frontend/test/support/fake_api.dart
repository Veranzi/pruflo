import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:pruflo/core/api_client.dart';
import 'package:pruflo/core/auth_service.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// A stand-in for the Cloud Run API that answers with contract-shaped JSON
/// (Docs/API_Contract.md and Docs/Integrations_API_Contract.md).
///
/// Data here exists only for tests; the app itself has no sample data.
class FakeApi {
  FakeApi({this.role = 'admin', this.empty = false});

  /// Role of the signed-in person returned by GET /v1/me.
  final String role;

  /// When true, every list is empty (a brand-new organisation).
  final bool empty;

  /// Every request the app made, in order.
  final requests = <http.Request>[];

  /// Replace any route's response: key is e.g. `GET /v1/me`.
  final overrides = <String, http.Response Function(http.Request)>{};

  late final client = MockClient(_handle);

  static const _at = '2026-10-16T09:30:00Z';

  static http.Response json(Object body, [int status = 200]) => http.Response(
        jsonEncode(body),
        status,
        headers: {'content-type': 'application/json'},
      );

  static http.Response error(int status, String code, String message) =>
      json({
        'error': {
          'code': code,
          'message': message,
          'field': null,
          'request_id': 'req_test'
        }
      }, status);

  static Map<String, dynamic> page(List<Object> items) =>
      {'items': items, 'next_cursor': null};

  /// Saved with PATCH /v1/me; merged like the API does.
  final preferences = <String, bool>{
    'evidence_processed': true,
    'case_assigned': true,
    'case_in_review': true,
    'report_ready': true,
    'weekly_summary': false,
    'push': true,
  };

  Map<String, dynamic> get me => {
        'id': 'u_test',
        'name': 'Test Reviewer',
        'email': 'tester@example.com',
        'role': role,
        'tenant_id': 'ten_test',
        'phone': '',
        'job_title': '',
        'active': true,
        'avatar_url': null,
        'notification_preferences': preferences,
      };

  /// Two unread, one read (endpoint 51).
  late final notifications = <Map<String, dynamic>>[
    _notification('ntf_1', 'case_assigned', 'T-001 was assigned to you', false),
    _notification('ntf_2', 'report_ready', 'T-001 Detailed report is ready', false),
    _notification('ntf_3', 'evidence_processed', 'order.pdf was processed', true),
  ];

  static Map<String, dynamic> _notification(
          String id, String kind, String title, bool read) =>
      {
        'id': id,
        'kind': kind,
        'title': title,
        'body': 'Test case T-001',
        'case_id': 'case_1',
        'case_reference': 'T-001',
        'link': '/cases/case_1',
        'read': read,
        'created_at': _at,
      };

  int get _unread => notifications.where((n) => n['read'] != true).length;

  final customers = <Map<String, dynamic>>[
    {
      'id': 'cus_1',
      'name': 'Acme Supplies',
      'contact_email': 'ops@acme.test',
      'created_at': _at,
    },
  ];

  /// Members of case_1 (endpoints 31, 32).
  var memberIds = <String>['u_test'];

  static const _orgUsers = [
    {'id': 'u_test', 'name': 'Test Reviewer', 'email': 'tester@example.com'},
    {'id': 'u_other', 'name': 'Other Investigator', 'email': 'other@example.com'},
  ];

  static Map<String, dynamic> caseJson(String id, String ref, String status) =>
      {
        'id': id,
        'reference': ref,
        'title': 'Test case $ref',
        'category': 'Procurement',
        'description': 'Check the delivery against the order.',
        'status': status,
        'priority': 'high',
        'customer_id': null,
        'customer_name': null,
        'assigned_to': {'uid': 'u_test', 'name': 'Test Reviewer'},
        'created_at': _at,
        'updated_at': _at,
        'version': 1,
      };

  static final cases = [
    caseJson('case_1', 'T-001', 'in_review'),
    caseJson('case_2', 'T-002', 'awaiting_evidence'),
  ];

  static final claim = {
    'id': 'clm_1',
    'evidence_id': 'evd_1',
    'file_name': 'order.pdf',
    'field': 'ordered_quantity',
    'value': '100',
    'unit': 'units',
    'page': 1,
    'quote': 'Qty ordered: 100',
    'grounding': 'grounded',
    'bbox': null,
    'model': 'test-model',
    'created_at': _at,
  };

  Map<String, dynamic> finding = {
    'id': 'fnd_1',
    'rule_id': 'rule_qty',
    'rule_version': 1,
    'type': 'quantity_shortfall',
    'severity': 'high',
    'expected': {'value': '100', 'unit': 'units'},
    'observed': {'value': '80', 'unit': 'units'},
    'difference': {'value': '20', 'unit': 'units'},
    'explanation': 'The order lists 100 units; the delivery note records 80.',
    'explanation_source': 'template',
    'review': {
      'status': 'pending',
      'by': null,
      'at': null,
      'note': '',
      'correction': null,
    },
    'evidence': [claim],
    'created_at': _at,
  };

  static final evidence = {
    'id': 'evd_1',
    'case_id': 'case_1',
    'file_name': 'order.pdf',
    'content_type': 'application/pdf',
    'size_bytes': 52144,
    'page_count': 2,
    'evidence_types': ['documents'],
    'status': 'processed',
    'error_class': null,
    'sha256': 'abc',
    'uploaded_at': _at,
    'created_at': _at,
  };

  static final timeline = {
    'id': 'aud_1',
    'action': 'case.created',
    'at': _at,
    'actor_type': 'person',
    'actor': {'uid': 'u_test', 'name': 'Test Reviewer'},
    'details': {'reference': 'T-001'},
  };

  static final report = {
    'id': 'rpt_1',
    'case_id': 'case_1',
    'title': 'T-001 Detailed report',
    'type': 'detailed',
    'format': 'pdf',
    'status': 'ready',
    'requested_by': {'uid': 'u_test', 'name': 'Test Reviewer'},
    'created_at': _at,
    'completed_at': _at,
  };

  List<Object> _list(List<Object> items) => empty ? <Object>[] : items;

  Future<http.Response> _handle(http.Request r) async {
    requests.add(r);
    final path = r.url.path;
    final key = '${r.method} $path';
    final override = overrides[key];
    if (override != null) return override(r);

    final caseMatch = RegExp(r'^/v1/cases/([^/]+)(/.*)?$').firstMatch(path);
    final sub = caseMatch?.group(2) ?? '';
    switch ((r.method, path)) {
      case ('GET', '/v1/me'):
        return json(me);
      case ('PATCH', '/v1/me'):
        final body = jsonDecode(r.body) as Map<String, dynamic>;
        final prefs = body.remove('notification_preferences') as Map?;
        prefs?.forEach((k, v) => preferences[k as String] = v as bool);
        return json({...me, ...body});
      case ('GET', '/v1/me/notifications'):
        final limit = int.tryParse(r.url.queryParameters['limit'] ?? '') ?? 20;
        final items = _list(notifications).take(limit).toList();
        return json({
          'items': items,
          'next_cursor': null,
          'unread_count': empty ? 0 : _unread,
        });
      case ('POST', '/v1/me/notifications/read-all'):
        final updated = _unread;
        for (final n in notifications) {
          n['read'] = true;
        }
        return json({'updated': updated});
      case ('POST', '/v1/me/avatar/upload-url'):
        return json({
          'upload_url': 'http://api.test/upload/avatar',
          'method': 'PUT',
          'headers': {'Content-Type': 'image/png'},
          'expires_at': _at,
        }, 201);
      case ('PUT', '/upload/avatar'):
        return http.Response('', 200);
      case ('POST', '/v1/me/avatar/complete'):
        return json({...me, 'avatar_url': 'http://api.test/avatar.jpg'});
      case ('DELETE', '/v1/me/avatar'):
        return http.Response('', 204);
      case ('GET', '/v1/customers'):
        return json(page(_list([
          for (final c in customers) {'id': c['id'], 'name': c['name']}
        ])));
      case ('GET', '/v1/admin/customers'):
        return json(page(_list(customers)));
      case ('POST', '/v1/admin/customers'):
        final body = jsonDecode(r.body) as Map<String, dynamic>;
        final created = {
          'id': 'cus_${customers.length + 1}',
          'name': body['name'],
          'contact_email': body['contact_email'] ?? '',
          'created_at': _at,
        };
        customers.insert(0, created);
        return json(created, 201);
      case ('GET', '/v1/admin/audit'):
        return json(page(_list([
          {
            'id': 'aud_1',
            'action': 'user.invited',
            'at': _at,
            'actor_type': 'person',
            'actor': {'uid': 'u_test', 'name': 'Test Reviewer'},
            'target_type': 'user',
            'target_id': 'u_other',
            'case_id': null,
            'details': {'email': 'other@example.com'},
          },
          {
            'id': 'aud_2',
            'action': 'case.status_changed',
            'at': _at,
            'actor_type': 'system',
            'actor': null,
            'target_type': 'case',
            'target_id': 'case_1',
            'case_id': 'case_1',
            'details': {'from': 'awaiting_evidence', 'to': 'in_review'},
          },
        ])));
      case ('GET', '/v1/admin/cases/case_1/members'):
        return json(_members());
      case ('PUT', '/v1/admin/cases/case_1/members'):
        memberIds = [
          for (final id in (jsonDecode(r.body) as Map)['user_ids'] as List)
            id as String
        ];
        return json(_members());
      case ('GET', '/v1/me/activity'):
        return json(page(_list([
          {
            'title': 'Uploaded order.pdf',
            'case_id': 'case_1',
            'case_reference': 'T-001',
            'at': _at,
            'kind': 'evidence_uploaded',
          }
        ])));
      case ('GET', '/v1/cases'):
        return json(page(_list(cases)));
      case ('GET', '/v1/admin/users'):
        return json(page(_list([
          for (final u in _orgUsers)
            {
              ...u,
              'role': u['id'] == 'u_test' ? role : 'investigator',
              'active': true,
              'signed_in': true,
              'created_at': _at,
            }
        ])));
      case ('GET', '/v1/admin/providers'):
        return json({
          'ocr': 'google_document_ai',
          'ai': 'vertex_gemini',
          'model': 'gemini-2.5-flash',
          'region': 'asia-southeast1',
        });
      case ('GET', '/v1/admin/api-keys'):
        return json(page(_list([
          {
            'id': 'key_1',
            'name': 'Test key',
            'prefix': 'pf_live_abcd',
            'scopes': ['cases:read'],
            'created_at': _at,
            'created_by': {'uid': 'u_test', 'name': 'Test Reviewer'},
            'last_used_at': null,
            'revoked': false,
          }
        ])));
      case ('GET', '/v1/admin/webhooks'):
        return json(page(_list([
          {
            'id': 'wh_1',
            'url': 'https://receiver.test/hooks',
            'description': 'Test receiver',
            'events': ['case.in_review'],
            'active': true,
            'created_at': _at,
            'recent_deliveries': [
              {
                'id': 'dlv_1',
                'event': 'ping',
                'status_code': 200,
                'attempt': 1,
                'at': _at,
              }
            ],
          }
        ])));
      case ('GET', '/v1/admin/connectors'):
        return json({
          'items': [
            for (final k in [
              'email_inbox',
              'google_drive',
              'sharepoint',
              'odoo',
              'sap'
            ])
              {
                'kind': k,
                'status': 'coming_soon',
                'detail': '',
                'connected_at': null,
              }
          ]
        });
      case ('GET', '/v1/admin/rules'):
        return json({
          'items': _list([
            {
              'id': 'rule_qty',
              'template': 'quantity_shortfall',
              'name': 'Quantity shortfall',
              'description': 'Ordered quantity vs delivered quantity.',
              'version': 1,
              'enabled': true,
              'params': [
                {
                  'key': 'tolerance_units',
                  'label': 'Allowed difference',
                  'value': '0',
                  'suffix': 'units',
                }
              ],
            }
          ])
        });
    }
    final read = RegExp(r'^/v1/me/notifications/([^/]+)/read$').firstMatch(path);
    if (r.method == 'POST' && read != null) {
      final n = notifications.where((n) => n['id'] == read.group(1)).firstOrNull;
      if (n == null) return error(404, 'not_found', 'Notification not found.');
      n['read'] = true;
      return json(n);
    }
    final customer = RegExp(r'^/v1/admin/customers/([^/]+)$').firstMatch(path);
    if (r.method == 'PATCH' && customer != null) {
      final c = customers.firstWhere((c) => c['id'] == customer.group(1));
      c.addAll(jsonDecode(r.body) as Map<String, dynamic>);
      return json(c);
    }
    if (caseMatch != null) {
      final id = caseMatch.group(1)!;
      final aCase = cases.where((c) => c['id'] == id).firstOrNull;
      if (aCase == null) return error(404, 'not_found', 'Case not found.');
      if (r.method == 'GET' && sub.isEmpty) return json(aCase);
      if (r.method == 'GET' && sub == '/evidence') {
        return json(page(_list([evidence])));
      }
      if (r.method == 'GET' && sub == '/timeline') {
        return json(page(_list([timeline])));
      }
      if (r.method == 'GET' && sub == '/findings') {
        return json(page(_list([finding])));
      }
      if (r.method == 'GET' && sub == '/claims') {
        return json(page(_list([claim])));
      }
      if (r.method == 'GET' && sub == '/reports') {
        return json(page(_list([report])));
      }
      if (r.method == 'POST' && sub == '/reports') {
        final body = jsonDecode(r.body) as Map<String, dynamic>;
        return json({
          ...report,
          'id': 'rpt_2',
          'format': body['format'] ?? 'pdf',
          'status': 'queued',
        }, 202);
      }
      if (r.method == 'GET' && sub.endsWith('/download-url')) {
        return json({
          'url': 'https://files.test/${r.url.queryParameters['disposition']}',
          'expires_at': _at,
        });
      }
      if (r.method == 'POST' && sub.endsWith('/decision')) {
        final body = jsonDecode(r.body) as Map<String, dynamic>;
        finding = {
          ...finding,
          'review': {
            'status': body['decision'],
            'by': {'uid': 'u_test', 'name': 'Test Reviewer'},
            'at': _at,
            'note': body['note'],
            'correction': null,
          },
        };
        return json(finding);
      }
    }
    return error(404, 'not_found', 'No route for $key in the fake API.');
  }

  Map<String, dynamic> _members() => {
        'items': [
          for (final u in _orgUsers)
            if (memberIds.contains(u['id']))
              {
                'uid': u['id'],
                'name': u['name'],
                'email': u['email'],
                'role': u['id'] == 'u_test' ? role : 'investigator',
              }
        ]
      };

  /// Point the app at this fake and reset the session. [storage] is what the
  /// browser had saved before launch (e.g. a development sign-in).
  Future<void> install({Map<String, Object> storage = const {}}) async {
    SharedPreferences.setMockInitialValues({});
    await AuthService.instance.signOut();
    SharedPreferences.setMockInitialValues(storage);
    ApiClient.instance = ApiClient(httpClient: client, baseUrl: 'http://api.test');
    await AuthService.instance.init();
  }

  /// Requests matching [method] and a path ending in [suffix].
  List<http.Request> sent(String method, String suffix) => [
        for (final r in requests)
          if (r.method == method && r.url.path.endsWith(suffix)) r,
      ];
}
