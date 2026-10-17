/// Models for the Integrations settings (API keys, webhooks, connectors,
/// reconciliation rules and processing providers).
///
/// Field names follow the JSON contract in
/// Docs/Integrations_API_Contract.md.
library;

DateTime _date(Object? v) => DateTime.parse(v as String).toLocal();
DateTime? _dateOrNull(Object? v) => v == null ? null : _date(v);

T _byWire<T extends Enum>(List<T> values, String Function(T) wire, Object? v,
        T fallback) =>
    values.firstWhere((e) => wire(e) == v, orElse: () => fallback);

/// Permission a system-to-system API key can be given.
enum ApiScope {
  casesRead('cases:read', 'Read cases'),
  casesWrite('cases:write', 'Create and update cases'),
  evidenceWrite('evidence:write', 'Upload evidence'),
  findingsRead('findings:read', 'Read findings and claims'),
  reportsRead('reports:read', 'Download reports and exports');

  const ApiScope(this.wire, this.label);

  /// Value sent to and received from the API.
  final String wire;
  final String label;
}

class ApiKey {
  const ApiKey({
    required this.id,
    required this.name,
    required this.prefix,
    required this.scopes,
    required this.createdAt,
    required this.createdBy,
    this.lastUsedAt,
    this.revoked = false,
  });

  factory ApiKey.fromJson(Map<String, dynamic> j) => ApiKey(
        id: j['id'] as String,
        name: j['name'] as String,
        prefix: j['prefix'] as String,
        scopes: {
          for (final s in j['scopes'] as List)
            for (final scope in ApiScope.values)
              if (scope.wire == s) scope,
        },
        createdAt: _date(j['created_at']),
        createdBy: ((j['created_by'] as Map?)?['name'] as String?) ?? '',
        lastUsedAt: _dateOrNull(j['last_used_at']),
        revoked: (j['revoked'] as bool?) ?? false,
      );

  final String id;
  final String name;

  /// First characters of the key, shown so admins can tell keys apart.
  /// The full secret is returned once, on creation, and never again.
  final String prefix;
  final Set<ApiScope> scopes;
  final DateTime createdAt;
  final String createdBy;
  final DateTime? lastUsedAt;
  final bool revoked;
}

/// Events PruFlo sends to webhook endpoints.
enum WebhookEvent {
  evidenceProcessed('evidence.processed', 'Evidence processed'),
  evidenceFailed('evidence.failed', 'Evidence processing failed'),
  caseInReview('case.in_review', 'Case needs review'),
  findingDecided(
    'finding.decided',
    'Finding approved, rejected or set to pending',
  ),
  caseCompleted('case.completed', 'Case review completed'),
  reportReady('report.ready', 'Report or export ready');

  const WebhookEvent(this.wire, this.label);
  final String wire;
  final String label;
}

class WebhookDelivery {
  const WebhookDelivery({
    required this.event,
    required this.statusCode,
    required this.at,
    this.attempt = 1,
  });

  factory WebhookDelivery.fromJson(Map<String, dynamic> j) => WebhookDelivery(
        event: j['event'] as String,
        statusCode: (j['status_code'] as int?) ?? 0,
        at: _date(j['at']),
        attempt: (j['attempt'] as int?) ?? 1,
      );

  /// Event wire value, e.g. `finding.decided` or `ping`.
  final String event;

  /// HTTP status returned by the receiver; 0 when it could not be reached.
  final int statusCode;
  final DateTime at;
  final int attempt;

  bool get succeeded => statusCode >= 200 && statusCode < 300;
}

class WebhookEndpoint {
  const WebhookEndpoint({
    required this.id,
    required this.url,
    required this.events,
    required this.createdAt,
    this.description = '',
    this.active = true,
    this.deliveries = const [],
  });

  factory WebhookEndpoint.fromJson(Map<String, dynamic> j) => WebhookEndpoint(
        id: j['id'] as String,
        url: j['url'] as String,
        description: (j['description'] as String?) ?? '',
        events: {
          for (final e in j['events'] as List)
            for (final ev in WebhookEvent.values)
              if (ev.wire == e) ev,
        },
        active: (j['active'] as bool?) ?? true,
        createdAt: _date(j['created_at']),
        deliveries: [
          for (final d in (j['recent_deliveries'] as List?) ?? const [])
            WebhookDelivery.fromJson(d as Map<String, dynamic>),
        ],
      );

  final String id;
  final String url;
  final String description;
  final Set<WebhookEvent> events;
  final bool active;
  final DateTime createdAt;

  /// Most recent first.
  final List<WebhookDelivery> deliveries;
}

/// Places documents can be pulled in from.
enum ConnectorKind {
  emailInbox(
    'email_inbox',
    'Email inbox',
    'Send or forward documents from your PruFlo account\'s email address; '
    'they are added to a case (put [PL-001] in the subject for an existing one).',
  ),
  googleDrive(
    'google_drive',
    'Google Drive',
    'Import files from a shared Drive folder.',
  ),
  sharePoint(
    'sharepoint',
    'SharePoint / OneDrive',
    'Import files from a SharePoint document library.',
  ),
  odoo(
    'odoo',
    'Odoo',
    'Pull purchase orders, delivery notes and vendor bills from Odoo.',
  ),
  sap(
    'sap',
    'SAP S/4HANA',
    'Pull purchase orders, goods receipts and invoices from SAP.',
  );

  const ConnectorKind(this.wire, this.label, this.description);
  final String wire;
  final String label;
  final String description;
}

enum ConnectorStatus {
  notConnected('not_connected'),
  connected('connected'),
  error('error'),
  comingSoon('coming_soon');

  const ConnectorStatus(this.wire);
  final String wire;
}

class Connector {
  const Connector({
    required this.kind,
    required this.status,
    this.detail = '',
    this.connectedAt,
  });

  /// Null for kinds this app doesn't know yet (the API may add some).
  static Connector? fromJson(Map<String, dynamic> j) {
    final kind = ConnectorKind.values.where((k) => k.wire == j['kind']);
    if (kind.isEmpty) return null;
    return Connector(
      kind: kind.first,
      status: _byWire(ConnectorStatus.values, (s) => s.wire, j['status'],
          ConnectorStatus.comingSoon),
      detail: (j['detail'] as String?) ?? '',
      connectedAt: _dateOrNull(j['connected_at']),
    );
  }

  final ConnectorKind kind;
  final ConnectorStatus status;

  /// Inbox address, folder name, Odoo URL or error message.
  final String detail;
  final DateTime? connectedAt;
}

/// One configurable input of a rule, such as a tolerance.
class RuleParam {
  const RuleParam({
    required this.key,
    required this.label,
    required this.value,
    this.suffix = '',
  });

  factory RuleParam.fromJson(Map<String, dynamic> j) => RuleParam(
        key: j['key'] as String,
        label: (j['label'] as String?) ?? j['key'] as String,
        value: '${j['value'] ?? ''}',
        suffix: (j['suffix'] as String?) ?? '',
      );

  final String key;
  final String label;

  /// Kept as a string; the API validates and stores it as a decimal.
  final String value;
  final String suffix;

  RuleParam withValue(String v) =>
      RuleParam(key: key, label: label, value: v, suffix: suffix);
}

/// A deterministic reconciliation rule. Code does the calculation; the
/// tenant only chooses which rules run and their parameters.
class ReconciliationRule {
  const ReconciliationRule({
    required this.id,
    required this.template,
    required this.name,
    required this.description,
    required this.version,
    required this.params,
    this.enabled = true,
  });

  factory ReconciliationRule.fromJson(Map<String, dynamic> j) =>
      ReconciliationRule(
        id: j['id'] as String,
        template: j['template'] as String,
        name: (j['name'] as String?) ?? j['template'] as String,
        description: (j['description'] as String?) ?? '',
        version: (j['version'] as int?) ?? 1,
        enabled: (j['enabled'] as bool?) ?? true,
        params: [
          for (final p in (j['params'] as List?) ?? const [])
            RuleParam.fromJson(p as Map<String, dynamic>),
        ],
      );

  final String id;

  /// Rule implementation in the backend, e.g. `quantity_shortfall`.
  final String template;
  final String name;
  final String description;

  /// Increases on every parameter change so findings stay reproducible.
  final int version;
  final List<RuleParam> params;
  final bool enabled;

}

enum OcrProvider {
  googleDocumentAi('google_document_ai', 'Google Document AI'),
  azureDocumentIntelligence(
    'azure_document_intelligence',
    'Azure AI Document Intelligence',
  ),
  awsTextract('aws_textract', 'AWS Textract'),
  selfHosted('self_hosted', 'Self-hosted (Tesseract)');

  const OcrProvider(this.wire, this.label);
  final String wire;
  final String label;
}

enum AiProvider {
  vertexGemini('vertex_gemini', 'Gemini on Vertex AI'),
  geminiApi('gemini_api', 'Gemini API (paid key)'),
  vertexClaude('vertex_claude', 'Claude on Vertex AI'),
  azureOpenAi('azure_openai', 'Azure OpenAI'),
  selfHosted('self_hosted', 'Self-hosted model');

  const AiProvider(this.wire, this.label);
  final String wire;
  final String label;
}

class ProcessingRegion {
  const ProcessingRegion(this.id, this.label);
  final String id;
  final String label;
}

class ProviderSettings {
  const ProviderSettings({
    required this.ocr,
    required this.ai,
    required this.model,
    required this.region,
  });

  factory ProviderSettings.fromJson(Map<String, dynamic> j) =>
      ProviderSettings(
        ocr: _byWire(OcrProvider.values, (p) => p.wire, j['ocr'],
            OcrProvider.googleDocumentAi),
        ai: _byWire(AiProvider.values, (p) => p.wire, j['ai'],
            AiProvider.vertexGemini),
        model: j['model'] as String,
        region: j['region'] as String,
      );

  Map<String, dynamic> toJson() =>
      {'ocr': ocr.wire, 'ai': ai.wire, 'model': model, 'region': region};

  final OcrProvider ocr;
  final AiProvider ai;

  /// Model ID from configuration, never hard-coded in the worker.
  final String model;
  final String region;
}

/// Regions offered for processing (Docs/backend_plan.md, Step 0).
const processingRegions = [
  ProcessingRegion('asia-southeast1', 'Singapore (asia-southeast1)'),
  ProcessingRegion('asia-southeast2', 'Jakarta (asia-southeast2)'),
];

String regionLabel(String id) => processingRegions
    .firstWhere((r) => r.id == id, orElse: () => ProcessingRegion(id, id))
    .label;

/// Models the API accepts (a server-side allowlist).
const allowedModels = ['gemini-2.5-flash', 'gemini-2.5-pro'];
