import 'dart:typed_data';

import 'api_client.dart';
import 'api_endpoints.dart';
import 'models.dart';

/// Content types the backend expects per extension
/// (backend/app/storage/file_types.py).
const evidenceContentTypes = {
  'pdf': 'application/pdf',
  'docx':
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'csv': 'text/csv',
  'txt': 'text/plain',
  'jpg': 'image/jpeg',
  'jpeg': 'image/jpeg',
  'png': 'image/png',
  'tif': 'image/tiff',
  'tiff': 'image/tiff',
};

/// Typed calls for endpoints 1–33 and 50 (Docs/API_Contract.md).
/// Every screen gets its data from here; there is no local sample data.
class PrufloApi {
  PrufloApi([ApiClient? client]) : _client = client;

  final ApiClient? _client;
  ApiClient get _api => _client ?? ApiClient.instance;

  // Profile ------------------------------------------------------------------

  /// 1
  Future<AppUser> me() async => AppUser.fromJson(await _api.get(ApiEndpoints.me));

  /// 21: name, phone, job title and notification preferences; role and email
  /// are managed by admins. Preferences are merged, so send only the changes.
  Future<AppUser> updateMe({
    String? name,
    String? phone,
    String? jobTitle,
    Map<String, bool>? notificationPreferences,
  }) async =>
      AppUser.fromJson(await _api.patch(ApiEndpoints.me, {
        'name': ?name,
        'phone': ?phone,
        'job_title': ?jobTitle,
        'notification_preferences': ?notificationPreferences,
      }));

  /// 23
  Future<List<ActivityItem>> activity({int limit = 10}) async {
    final page =
        await _api.get(ApiEndpoints.myActivity, query: {'limit': '$limit'});
    return Page.fromJson(page, ActivityItem.fromJson).items;
  }

  // Cases --------------------------------------------------------------------

  /// 3
  Future<Page<CaseItem>> cases({
    CaseStatus? status,
    Priority? priority,
    String? query,
    String? cursor,
    int limit = 50,
  }) async {
    final page = await _api.get(ApiEndpoints.cases, query: {
      'limit': '$limit',
      'status': ?status?.wire,
      'priority': ?priority?.name,
      if (query != null && query.trim().isNotEmpty) 'q': query.trim(),
      'cursor': ?cursor,
    });
    return Page.fromJson(page, CaseItem.fromJson);
  }

  /// Every case the caller can see, following cursors up to [cap] cases.
  Future<(List<CaseItem>, bool truncated)> allCases({int cap = 1000}) async {
    final all = <CaseItem>[];
    String? cursor;
    do {
      final page = await cases(cursor: cursor, limit: 200);
      all.addAll(page.items);
      cursor = page.nextCursor;
    } while (cursor != null && all.length < cap);
    return (all, cursor != null);
  }

  /// 4
  Future<CaseItem> createCase({
    required String title,
    String category = '',
    String description = '',
    Priority priority = Priority.medium,
    String? assignedTo,
    String? customerId,
  }) async =>
      CaseItem.fromJson(await _api.post(ApiEndpoints.cases, {
        'title': title,
        'category': category,
        'description': description,
        'priority': priority.name,
        'assigned_to': ?assignedTo,
        'customer_id': ?customerId,
      }));

  /// 5
  Future<CaseItem> getCase(String caseId) async =>
      CaseItem.fromJson(await _api.get(ApiEndpoints.caseById(caseId)));

  /// 6: send the [version] you edited to be told (409) if someone else changed it.
  Future<CaseItem> updateCase(String caseId, Map<String, dynamic> changes,
          {int? version}) async =>
      CaseItem.fromJson(await _api.patch(ApiEndpoints.caseById(caseId), {
        ...changes,
        'version': ?version,
      }));

  /// 16
  Future<List<TimelineItem>> timeline(String caseId, {int limit = 100}) async {
    final page = await _api
        .get(ApiEndpoints.timeline(caseId), query: {'limit': '$limit'});
    return Page.fromJson(page, TimelineItem.fromJson).items;
  }

  // Evidence -----------------------------------------------------------------

  /// 9
  Future<List<EvidenceItem>> evidence(String caseId, {int limit = 100}) async {
    final page = await _api
        .get(ApiEndpoints.evidence(caseId), query: {'limit': '$limit'});
    return Page.fromJson(page, EvidenceItem.fromJson).items;
  }

  /// 10
  Future<EvidenceItem> retryEvidence(String caseId, String evidenceId) async =>
      EvidenceItem.fromJson(
          await _api.post(ApiEndpoints.evidenceRetry(caseId, evidenceId)));

  /// 11: a short-lived link to the original file.
  Future<String> evidenceViewUrl(String caseId, String evidenceId) async =>
      (await _api.get(ApiEndpoints.evidenceViewUrl(caseId, evidenceId)))['url']
          as String;

  /// Ask for a signed URL (7), PUT the bytes, then complete (8) so the server
  /// verifies the file and queues processing. [onStep] reports 0..1.
  Future<EvidenceItem> upload({
    required String caseId,
    required String fileName,
    required Uint8List bytes,
    required List<String> evidenceTypes,
    void Function(double)? onStep,
  }) async {
    final ext = fileName.split('.').last.toLowerCase();
    final slot = await _api.post(ApiEndpoints.uploadUrl(caseId), {
      'file_name': fileName,
      'size_bytes': bytes.length,
      'content_type': evidenceContentTypes[ext] ?? 'application/octet-stream',
      'evidence_types': evidenceTypes,
    });
    onStep?.call(0.2);
    await _api.putSigned(
      slot['upload_url'] as String,
      bytes,
      Map<String, String>.from(slot['headers'] as Map),
    );
    onStep?.call(0.8);
    final done = await _api.post(ApiEndpoints.evidenceComplete(
        caseId, slot['evidence_id'] as String));
    onStep?.call(1);
    return EvidenceItem.fromJson(done);
  }

  // Claims, findings and review ----------------------------------------------

  /// 13
  Future<List<Claim>> claims(String caseId) async {
    final page =
        await _api.get(ApiEndpoints.claims(caseId), query: {'limit': '200'});
    return Page.fromJson(page, Claim.fromJson).items;
  }

  /// 14
  Future<List<Finding>> findings(String caseId) async {
    final page =
        await _api.get(ApiEndpoints.findings(caseId), query: {'limit': '200'});
    return Page.fromJson(page, Finding.fromJson).items;
  }

  /// 15: people only. `reject` and `pending` need a note.
  Future<Finding> decide(String caseId, String findingId,
          ReviewDecision decision, String note) async =>
      Finding.fromJson(await _api.post(
          ApiEndpoints.decision(caseId, findingId),
          {'decision': decision.wire, 'note': note}));

  /// 20: optional case Q&A. Throws [ApiException] with code
  /// `assistant_unavailable` (503) when Gemini isn't set up on the server.
  Future<CaseAnswer> ask(String caseId, String question) async =>
      CaseAnswer.fromJson(
          await _api.post(ApiEndpoints.qa(caseId), {'question': question}));

  // Reports and export -------------------------------------------------------

  /// 17: generated by the worker; poll [reports] until it's ready.
  Future<ReportItem> requestReport(
          String caseId, String type, List<String> sections) async =>
      ReportItem.fromJson(await _api.post(
          ApiEndpoints.reports(caseId), {'type': type, 'sections': sections}));

  /// 18
  Future<List<ReportItem>> reports(String caseId) async {
    final page =
        await _api.get(ApiEndpoints.reports(caseId), query: {'limit': '50'});
    return Page.fromJson(page, ReportItem.fromJson).items;
  }

  /// 19
  Future<String> reportDownloadUrl(String caseId, String reportId) async =>
      (await _api.get(ApiEndpoints.reportDownload(caseId, reportId)))['url']
          as String;

  /// 50: the file body, `json` or `csv`.
  Future<String> exportFindings(String caseId, String format) =>
      _api.getText(ApiEndpoints.caseExport(caseId), query: {'format': format});

  // Admin --------------------------------------------------------------------

  /// 24
  Future<List<OrgUser>> users() async {
    final page =
        await _api.get(ApiEndpoints.adminUsers, query: {'limit': '200'});
    return Page.fromJson(page, OrgUser.fromJson).items;
  }

  /// 25: no email is sent; they sign in with this address.
  Future<OrgUser> inviteUser(
          {required String email,
          required String name,
          required UserRole role}) async =>
      OrgUser.fromJson(await _api.post(ApiEndpoints.adminUsers,
          {'email': email, 'name': name, 'role': role.name}));

  /// 26
  Future<OrgUser> updateUser(String userId,
          {UserRole? role, bool? active}) async =>
      OrgUser.fromJson(await _api.patch(ApiEndpoints.adminUser(userId), {
        'role': ?role?.name,
        'active': ?active,
      }));

  /// 27: removes them from this organisation.
  Future<void> removeUser(String userId) =>
      _api.delete(ApiEndpoints.adminUser(userId));

  /// 28
  Future<Page<Customer>> adminCustomers({String? cursor, int limit = 50}) async =>
      Page.fromJson(
          await _api.get(ApiEndpoints.adminCustomers,
              query: {'limit': '$limit', 'cursor': ?cursor}),
          Customer.fromJson);

  /// 29
  Future<Customer> createCustomer(
          {required String name, String contactEmail = ''}) async =>
      Customer.fromJson(await _api.post(ApiEndpoints.adminCustomers,
          {'name': name, 'contact_email': contactEmail}));

  /// 30
  Future<Customer> updateCustomer(String customerId,
          {String? name, String? contactEmail}) async =>
      Customer.fromJson(await _api.patch(ApiEndpoints.adminCustomer(customerId), {
        'name': ?name,
        'contact_email': ?contactEmail,
      }));

  /// 31
  Future<List<CaseMember>> caseMembers(String caseId) async {
    final body = await _api.get(ApiEndpoints.adminCaseMembers(caseId));
    return [
      for (final m in body['items'] as List)
        CaseMember.fromJson(m as Map<String, dynamic>)
    ];
  }

  /// 32: replaces the members with exactly these users.
  Future<List<CaseMember>> setCaseMembers(
      String caseId, List<String> userIds) async {
    final body = await _api
        .put(ApiEndpoints.adminCaseMembers(caseId), {'user_ids': userIds});
    return [
      for (final m in body['items'] as List)
        CaseMember.fromJson(m as Map<String, dynamic>)
    ];
  }

  /// 33: newest first.
  Future<Page<AuditEntry>> audit(
          {String? action, String? caseId, String? cursor, int limit = 50}) async =>
      Page.fromJson(
          await _api.get(ApiEndpoints.adminAudit, query: {
            'limit': '$limit',
            if (action != null && action.trim().isNotEmpty)
              'action': action.trim(),
            'case_id': ?caseId,
            'cursor': ?cursor,
          }),
          AuditEntry.fromJson);

  // Notifications, profile photo and customer picker -----------------------

  /// 51
  Future<NotificationPage> notifications(
          {bool unreadOnly = false, String? cursor, int limit = 20}) async =>
      NotificationPage.fromJson(await _api.get(ApiEndpoints.notifications,
          query: {
            'limit': '$limit',
            if (unreadOnly) 'unread_only': 'true',
            'cursor': ?cursor,
          }));

  /// 52
  Future<AppNotification> markNotificationRead(String id) async =>
      AppNotification.fromJson(
          await _api.post(ApiEndpoints.notificationRead(id)));

  /// 53
  Future<int> markAllNotificationsRead() async =>
      ((await _api.post(ApiEndpoints.notificationsReadAll))['updated']
          as int?) ??
      0;

  /// 54, PUT, 55: returns the profile with the new photo link.
  Future<AppUser> uploadAvatar(Uint8List bytes, String contentType) async {
    final slot = await _api.post(ApiEndpoints.avatarUploadUrl,
        {'content_type': contentType, 'size_bytes': bytes.length});
    await _api.putSigned(
      slot['upload_url'] as String,
      bytes,
      Map<String, String>.from(slot['headers'] as Map),
    );
    return AppUser.fromJson(await _api.post(ApiEndpoints.avatarComplete));
  }

  /// 56
  Future<void> removeAvatar() => _api.delete(ApiEndpoints.avatar);

  /// 57: any member may list customers, for pickers.
  Future<List<Customer>> customers() async {
    final page =
        await _api.get(ApiEndpoints.customers, query: {'limit': '200'});
    return Page.fromJson(page, Customer.fromJson).items;
  }

  /// 2: register a push token (FCM) for this device.
  Future<String> registerDevice(String token, {String platform = 'web'}) async =>
      (await _api.post(ApiEndpoints.devices,
          {'token': token, 'platform': platform}))['id'] as String;

  /// 22
  Future<void> removeDevice(String deviceId) =>
      _api.delete(ApiEndpoints.device(deviceId));
}
