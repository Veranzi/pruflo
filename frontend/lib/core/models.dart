/// Data returned by the API. Field names follow Docs/API_Contract.md.
library;

/// Case lifecycle. `wire` is the value the API sends and stores.
enum CaseStatus {
  awaitingEvidence('awaiting_evidence'),
  inReview('in_review'),
  completed('completed');

  const CaseStatus(this.wire);
  final String wire;

  static CaseStatus fromWire(String v) =>
      values.firstWhere((s) => s.wire == v, orElse: () => awaitingEvidence);
}

enum Priority {
  high,
  medium,
  low;

  static Priority fromWire(String v) =>
      values.firstWhere((p) => p.name == v, orElse: () => medium);
}

enum UserRole {
  investigator,
  reviewer,
  admin,
  viewer;

  static UserRole fromWire(String v) =>
      values.firstWhere((r) => r.name == v, orElse: () => viewer);

  /// Who may generate reports and exports (Docs/backend_plan.md, Step 0).
  bool get canExport => this != viewer;
  bool get canDecide => this == reviewer || this == admin;
  bool get canEditCases => this == investigator || this == admin;
}

/// A reviewer's decision on a finding. Every finding starts as `pending`.
/// `wire` is the value the API sends and stores.
enum ReviewDecision {
  approve('approve'),
  reject('reject'),
  pending('pending');

  const ReviewDecision(this.wire);
  final String wire;

  static ReviewDecision fromWire(String v) =>
      values.firstWhere((d) => d.wire == v, orElse: () => pending);
}

DateTime _date(Object? v) => DateTime.parse(v as String).toLocal();
DateTime? _dateOrNull(Object? v) => v == null ? null : _date(v);

/// `{"items": [...], "next_cursor": ...}`
class Page<T> {
  const Page(this.items, this.nextCursor);

  factory Page.fromJson(
          Map<String, dynamic> j, T Function(Map<String, dynamic>) item) =>
      Page(
        [for (final e in j['items'] as List) item(e as Map<String, dynamic>)],
        j['next_cursor'] as String?,
      );

  final List<T> items;
  final String? nextCursor;
}

/// `{"uid", "name"}` as used for assignees, actors and reviewers.
class PersonRef {
  const PersonRef(this.uid, this.name);

  static PersonRef? fromJson(Object? j) => j is Map
      ? PersonRef(j['uid'] as String, (j['name'] as String?) ?? '')
      : null;

  final String uid;
  final String name;
}

/// The signed-in person, from GET /v1/me.
class AppUser {
  const AppUser({
    required this.id,
    required this.name,
    required this.email,
    required this.role,
    this.active = true,
    this.phone = '',
    this.jobTitle = '',
    this.avatarUrl,
    this.notificationPreferences = const {},
  });

  factory AppUser.fromJson(Map<String, dynamic> j) => AppUser(
        id: j['id'] as String,
        name: (j['name'] as String?) ?? '',
        email: j['email'] as String,
        role: UserRole.fromWire(j['role'] as String),
        active: (j['active'] as bool?) ?? true,
        phone: (j['phone'] as String?) ?? '',
        jobTitle: (j['job_title'] as String?) ?? '',
        avatarUrl: j['avatar_url'] as String?,
        notificationPreferences: {
          for (final e in ((j['notification_preferences'] as Map?) ?? const {})
              .entries)
            e.key as String: e.value == true,
        },
      );

  AppUser copyWith({Map<String, bool>? notificationPreferences}) => AppUser(
        id: id,
        name: name,
        email: email,
        role: role,
        active: active,
        phone: phone,
        jobTitle: jobTitle,
        avatarUrl: avatarUrl,
        notificationPreferences:
            notificationPreferences ?? this.notificationPreferences,
      );

  final String id;
  final String name;
  final String email;
  final UserRole role;
  final bool active;
  final String phone;
  final String jobTitle;

  /// A short-lived link to the profile photo (about 10 minutes), or null.
  final String? avatarUrl;

  /// Which notifications this person gets, keyed by [NotificationKind.wire]
  /// plus `push`. See Docs/API_Contract.md, endpoints 51–57.
  final Map<String, bool> notificationPreferences;

  /// The name, or the email when no name has been set yet.
  String get displayName => name.trim().isNotEmpty ? name.trim() : email;

  String get initials {
    final parts = displayName
        .split(RegExp(r'[\s@.]+'))
        .where((p) => p.isNotEmpty)
        .take(2);
    return parts.map((p) => p[0].toUpperCase()).join();
  }

  String get firstName => displayName.split(' ').first;
}

class CaseItem {
  const CaseItem({
    required this.id,
    required this.reference,
    required this.title,
    required this.category,
    required this.description,
    required this.status,
    required this.priority,
    required this.createdAt,
    required this.updatedAt,
    required this.version,
    this.assignedTo,
    this.customerId,
    this.customerName,
  });

  factory CaseItem.fromJson(Map<String, dynamic> j) => CaseItem(
        id: j['id'] as String,
        reference: j['reference'] as String,
        title: j['title'] as String,
        category: (j['category'] as String?) ?? '',
        description: (j['description'] as String?) ?? '',
        status: CaseStatus.fromWire(j['status'] as String),
        priority: Priority.fromWire(j['priority'] as String),
        createdAt: _date(j['created_at']),
        updatedAt: _date(j['updated_at']),
        version: (j['version'] as int?) ?? 1,
        assignedTo: PersonRef.fromJson(j['assigned_to']),
        customerId: j['customer_id'] as String?,
        customerName: j['customer_name'] as String?,
      );

  /// Opaque API ID (case_...) used in routes and calls.
  final String id;

  /// What people see: the organisation's prefix and a number.
  final String reference;
  final String title;
  final String category;
  final String description;
  final CaseStatus status;
  final Priority priority;
  final DateTime createdAt;
  final DateTime updatedAt;
  final int version;
  final PersonRef? assignedTo;
  final String? customerId;
  final String? customerName;
}

/// An evidence file, from GET /v1/cases/{caseId}/evidence.
class EvidenceItem {
  const EvidenceItem({
    required this.id,
    required this.caseId,
    required this.fileName,
    required this.contentType,
    required this.sizeBytes,
    required this.status,
    required this.createdAt,
    this.pageCount,
    this.errorClass,
    this.uploadedAt,
  });

  factory EvidenceItem.fromJson(Map<String, dynamic> j) => EvidenceItem(
        id: j['id'] as String,
        caseId: (j['case_id'] as String?) ?? '',
        fileName: j['file_name'] as String,
        contentType: (j['content_type'] as String?) ?? '',
        sizeBytes: j['size_bytes'] as int,
        pageCount: j['page_count'] as int?,
        status: j['status'] as String,
        errorClass: j['error_class'] as String?,
        uploadedAt: _dateOrNull(j['uploaded_at']),
        createdAt: _date(j['created_at']),
      );

  final String id;
  final String caseId;
  final String fileName;
  final String contentType;
  final int sizeBytes;
  final int? pageCount;

  /// queued, extracting, processed, needs_review, failed_retryable, ...
  final String status;
  final String? errorClass;
  final DateTime? uploadedAt;
  final DateTime createdAt;

  /// Upper-case extension for icons: PDF, DOCX, CSV ...
  String get type =>
      fileName.contains('.') ? fileName.split('.').last.toUpperCase() : '';

  String get statusLabel => switch (status) {
        'uploading' => 'Uploading',
        'queued' || 'extracting' || 'grounding' || 'reconciling' =>
          'Processing',
        'processed' => 'Processed',
        // e.g. a scan while OCR isn't set up: the file is kept, a person checks it.
        'needs_review' => errorClass == 'ocr_not_configured'
            ? 'Needs review (scanned, no OCR yet)'
            : 'Needs review',
        'failed_retryable' => 'Retrying',
        'failed_terminal' => 'Failed',
        _ => status,
      };
}

/// One entry of a case's timeline (endpoint 16) or the audit log (33).
class TimelineItem {
  const TimelineItem({
    required this.id,
    required this.action,
    required this.at,
    required this.actorType,
    required this.details,
    this.actor,
  });

  factory TimelineItem.fromJson(Map<String, dynamic> j) => TimelineItem(
        id: j['id'] as String,
        action: j['action'] as String,
        at: _date(j['at']),
        actorType: (j['actor_type'] as String?) ?? 'system',
        actor: PersonRef.fromJson(j['actor']),
        details: Map<String, dynamic>.from((j['details'] as Map?) ?? const {}),
      );

  final String id;
  final String action; // e.g. case.created, evidence.uploaded
  final DateTime at;
  final String actorType; // person, api_key, system
  final PersonRef? actor;
  final Map<String, dynamic> details;

  String get who => actor?.name.isNotEmpty == true
      ? actor!.name
      : switch (actorType) {
          'api_key' => 'An API key',
          'system' => 'Pruflo',
          _ => 'Someone',
        };

  /// A readable sentence for the timeline.
  String get description {
    final title = details['title'];
    if (title is String && title.isNotEmpty) return '$title · $who';
    return switch (action) {
      'case.created' => 'Case created by $who',
      'case.updated' => 'Case details updated by $who',
      'case.status_changed' =>
        'Status changed to ${_statusWords(details['to'])} ($who)',
      'evidence.rejected' => 'A file was rejected: ${details['reason']}',
      'evidence.retried' => 'Processing retried by $who',
      'finding.decided' => 'Finding ${details['decision'] ?? 'decided'} by $who',
      'report.requested' => 'Report requested by $who',
      _ => '${action.replaceAll('.', ' ').replaceAll('_', ' ')} · $who',
    };
  }

  static String _statusWords(Object? wire) => switch (wire) {
        'in_review' => 'In Review',
        'awaiting_evidence' => 'Awaiting Evidence',
        'completed' => 'Completed',
        _ => '$wire',
      };
}

enum ActivityKind {
  evidenceUploaded('evidence_uploaded'),
  reviewCompleted('review_completed'),
  reportGenerated('report_generated');

  const ActivityKind(this.wire);
  final String wire;

  static ActivityKind fromWire(String v) =>
      values.firstWhere((k) => k.wire == v, orElse: () => evidenceUploaded);
}

/// The signed-in user's recent activity, from GET /v1/me/activity.
class ActivityItem {
  const ActivityItem({
    required this.title,
    required this.at,
    required this.kind,
    this.caseId,
    this.caseReference,
  });

  factory ActivityItem.fromJson(Map<String, dynamic> j) => ActivityItem(
        title: j['title'] as String,
        caseId: j['case_id'] as String?,
        caseReference: j['case_reference'] as String?,
        at: _date(j['at']),
        kind: ActivityKind.fromWire(j['kind'] as String),
      );

  final String title;
  final String? caseId;
  final String? caseReference;
  final DateTime at;
  final ActivityKind kind;
}

/// `{"value": "100", "unit": "units"}`
class Quantity {
  const Quantity(this.value, this.unit);

  static Quantity? fromJson(Object? j) => j is Map
      ? Quantity('${j['value'] ?? ''}', (j['unit'] as String?) ?? '')
      : null;

  final String value;
  final String unit;

  @override
  String toString() => unit.isEmpty ? value : '$value $unit';
}

/// A value read from a document, with where it came from (endpoint 13).
class Claim {
  const Claim({
    required this.id,
    required this.evidenceId,
    required this.fileName,
    required this.field,
    required this.value,
    required this.grounding,
    this.unit = '',
    this.page,
    this.quote = '',
  });

  factory Claim.fromJson(Map<String, dynamic> j) => Claim(
        id: (j['id'] ?? j['claim_id']) as String,
        evidenceId: j['evidence_id'] as String,
        fileName: (j['file_name'] as String?) ?? '',
        field: (j['field'] as String?) ?? '',
        value: '${j['value'] ?? ''}',
        unit: (j['unit'] as String?) ?? '',
        page: j['page'] as int?,
        quote: (j['quote'] as String?) ?? '',
        grounding: (j['grounding'] as String?) ?? 'needs_review',
      );

  final String id;
  final String evidenceId;
  final String fileName;
  final String field;
  final String value;
  final String unit;
  final int? page;
  final String quote;

  /// grounded, ambiguous, ungrounded or needs_review.
  final String grounding;

  String get fieldLabel => field.replaceAll('_', ' ');
}

class FindingReview {
  const FindingReview({
    required this.status,
    this.by,
    this.at,
    this.note = '',
  });

  factory FindingReview.fromJson(Map<String, dynamic> j) => FindingReview(
        status: ReviewDecision.fromWire((j['status'] as String?) ?? 'pending'),
        by: PersonRef.fromJson(j['by']),
        at: _dateOrNull(j['at']),
        note: (j['note'] as String?) ?? '',
      );

  final ReviewDecision status;
  final PersonRef? by;
  final DateTime? at;
  final String note;
}

/// A rule result for a person to review (endpoint 14).
class Finding {
  const Finding({
    required this.id,
    required this.type,
    required this.severity,
    required this.review,
    required this.evidence,
    required this.createdAt,
    this.ruleVersion,
    this.expected,
    this.observed,
    this.difference,
    this.explanation = '',
    this.explanationSource = 'template',
  });

  factory Finding.fromJson(Map<String, dynamic> j) => Finding(
        id: j['id'] as String,
        type: (j['type'] as String?) ?? '',
        severity: (j['severity'] as String?) ?? 'medium',
        ruleVersion: j['rule_version'] as int?,
        expected: Quantity.fromJson(j['expected']),
        observed: Quantity.fromJson(j['observed']),
        difference: Quantity.fromJson(j['difference']),
        explanation: (j['explanation'] as String?) ?? '',
        explanationSource: (j['explanation_source'] as String?) ?? 'template',
        review: FindingReview.fromJson(
            Map<String, dynamic>.from((j['review'] as Map?) ?? const {})),
        evidence: [
          for (final c in (j['evidence'] as List?) ?? const [])
            Claim.fromJson(c as Map<String, dynamic>),
        ],
        createdAt: _date(j['created_at']),
      );

  final String id;
  final String type; // e.g. quantity_shortfall
  final String severity; // high, medium, low
  final int? ruleVersion;
  final Quantity? expected;
  final Quantity? observed;
  final Quantity? difference;
  final String explanation;
  final String explanationSource; // template or machine_generated
  final FindingReview review;
  final List<Claim> evidence;
  final DateTime createdAt;

  String get title {
    final words = type.replaceAll('_', ' ');
    return words.isEmpty ? 'Finding' : words[0].toUpperCase() + words.substring(1);
  }
}

/// An answer from the case assistant (endpoint 20). Machine-generated: it
/// cites the findings and claims it's based on, and never decides anything.
class CaseAnswer {
  const CaseAnswer(this.answer, this.citations);

  factory CaseAnswer.fromJson(Map<String, dynamic> j) => CaseAnswer(
        (j['answer'] as String?) ?? '',
        [
          for (final c in (j['citations'] as List?) ?? const [])
            (type: (c as Map)['type'] as String, id: c['id'] as String),
        ],
      );

  final String answer;
  final List<({String type, String id})> citations;
}

/// A generated PDF report (endpoints 17–19).
class ReportItem {
  const ReportItem({
    required this.id,
    required this.caseId,
    required this.title,
    required this.status,
    required this.createdAt,
    this.type = '',
    this.completedAt,
    this.requestedBy,
  });

  factory ReportItem.fromJson(Map<String, dynamic> j) => ReportItem(
        id: j['id'] as String,
        caseId: j['case_id'] as String,
        title: (j['title'] as String?) ?? 'Report',
        type: (j['type'] as String?) ?? '',
        status: (j['status'] as String?) ?? 'queued',
        createdAt: _date(j['created_at']),
        completedAt: _dateOrNull(j['completed_at']),
        requestedBy: PersonRef.fromJson(j['requested_by']),
      );

  final String id;
  final String caseId;
  final String title;
  final String type;
  final String status; // queued, ready, failed
  final DateTime createdAt;
  final DateTime? completedAt;
  final PersonRef? requestedBy;

  bool get ready => status == 'ready';
  bool get queued => status == 'queued';
}

/// A person in the organisation, as admins see them (endpoint 24).
class OrgUser {
  const OrgUser({
    required this.id,
    required this.name,
    required this.email,
    required this.role,
    required this.active,
    required this.signedIn,
  });

  factory OrgUser.fromJson(Map<String, dynamic> j) => OrgUser(
        id: j['id'] as String,
        name: (j['name'] as String?) ?? '',
        email: j['email'] as String,
        role: UserRole.fromWire(j['role'] as String),
        active: (j['active'] as bool?) ?? true,
        signedIn: (j['signed_in'] as bool?) ?? false,
      );

  final String id;
  final String name;
  final String email;
  final UserRole role;
  final bool active;

  /// False until they first sign in with the invited email.
  final bool signedIn;

  String get displayName => name.trim().isNotEmpty ? name.trim() : email;

  String get initials {
    final parts = displayName
        .split(RegExp(r'[\s@.]+'))
        .where((p) => p.isNotEmpty)
        .take(2);
    return parts.map((p) => p[0].toUpperCase()).join();
  }
}

/// What a notification is about (Docs/API_Contract.md, endpoint 51).
enum NotificationKind {
  evidenceProcessed('evidence_processed'),
  caseAssigned('case_assigned'),
  caseInReview('case_in_review'),
  reportReady('report_ready'),
  weeklySummary('weekly_summary');

  const NotificationKind(this.wire);
  final String wire;

  static NotificationKind? fromWire(String v) =>
      values.where((k) => k.wire == v).firstOrNull;
}

/// One item in the notifications bell, from GET /v1/me/notifications.
class AppNotification {
  const AppNotification({
    required this.id,
    required this.kind,
    required this.title,
    required this.body,
    required this.read,
    required this.createdAt,
    this.caseId,
    this.caseReference,
    this.link,
  });

  factory AppNotification.fromJson(Map<String, dynamic> j) => AppNotification(
        id: j['id'] as String,
        kind: NotificationKind.fromWire(j['kind'] as String),
        title: j['title'] as String,
        body: (j['body'] as String?) ?? '',
        read: (j['read'] as bool?) ?? false,
        createdAt: _date(j['created_at']),
        caseId: j['case_id'] as String?,
        caseReference: j['case_reference'] as String?,
        link: j['link'] as String?,
      );

  AppNotification markedRead() => AppNotification(
        id: id,
        kind: kind,
        title: title,
        body: body,
        read: true,
        createdAt: createdAt,
        caseId: caseId,
        caseReference: caseReference,
        link: link,
      );

  final String id;

  /// Null for a kind this build doesn't know yet; it's still shown.
  final NotificationKind? kind;
  final String title;
  final String body;
  final bool read;
  final DateTime createdAt;
  final String? caseId;
  final String? caseReference;

  /// An app route such as `/cases/case_...`.
  final String? link;
}

/// GET /v1/me/notifications also returns how many are unread.
class NotificationPage {
  const NotificationPage(this.items, this.nextCursor, this.unreadCount);

  factory NotificationPage.fromJson(Map<String, dynamic> j) =>
      NotificationPage(
        [
          for (final e in j['items'] as List)
            AppNotification.fromJson(e as Map<String, dynamic>)
        ],
        j['next_cursor'] as String?,
        (j['unread_count'] as int?) ?? 0,
      );

  final List<AppNotification> items;
  final String? nextCursor;
  final int unreadCount;
}

/// A customer cases can be filed under (endpoints 28–30 and 57).
class Customer {
  const Customer({
    required this.id,
    required this.name,
    this.contactEmail = '',
    this.createdAt,
  });

  factory Customer.fromJson(Map<String, dynamic> j) => Customer(
        id: j['id'] as String,
        name: j['name'] as String,
        contactEmail: (j['contact_email'] as String?) ?? '',
        createdAt: _dateOrNull(j['created_at']),
      );

  final String id;
  final String name;
  final String contactEmail;

  /// Only in the admin list (28); the picker (57) leaves it out.
  final DateTime? createdAt;
}

/// A member of a case, from GET /v1/admin/cases/{caseId}/members (31).
class CaseMember {
  const CaseMember({
    required this.uid,
    required this.name,
    required this.email,
    required this.role,
  });

  factory CaseMember.fromJson(Map<String, dynamic> j) => CaseMember(
        uid: j['uid'] as String,
        name: (j['name'] as String?) ?? '',
        email: (j['email'] as String?) ?? '',
        role: UserRole.fromWire(j['role'] as String),
      );

  final String uid;
  final String name;
  final String email;
  final UserRole role;

  String get displayName => name.trim().isNotEmpty ? name.trim() : email;
}

/// One line of the organisation's audit log (33).
class AuditEntry {
  const AuditEntry({
    required this.id,
    required this.action,
    required this.at,
    required this.actorType,
    required this.targetType,
    required this.targetId,
    this.actor,
    this.caseId,
    this.details = const {},
  });

  factory AuditEntry.fromJson(Map<String, dynamic> j) => AuditEntry(
        id: j['id'] as String,
        action: j['action'] as String,
        at: _date(j['at']),
        actorType: (j['actor_type'] as String?) ?? 'system',
        actor: PersonRef.fromJson(j['actor']),
        targetType: (j['target_type'] as String?) ?? '',
        targetId: (j['target_id'] as String?) ?? '',
        caseId: j['case_id'] as String?,
        details: (j['details'] as Map?)?.cast<String, dynamic>() ?? const {},
      );

  final String id;

  /// e.g. `case.created`, `finding.decided`, `user.invited`.
  final String action;
  final DateTime at;

  /// `person`, `api_key` or `system`.
  final String actorType;
  final PersonRef? actor;
  final String targetType;
  final String targetId;
  final String? caseId;
  final Map<String, dynamic> details;

  /// Who did it, for people to read.
  String get actorLabel => switch (actorType) {
        'person' => actor?.name.isNotEmpty == true ? actor!.name : 'Someone',
        'api_key' => 'API key',
        _ => 'System',
      };

  /// `case.status_changed` reads as "Case status changed".
  String get actionLabel {
    final words = action.replaceAll('.', ' ').replaceAll('_', ' ').trim();
    return words.isEmpty ? action : words[0].toUpperCase() + words.substring(1);
  }
}
