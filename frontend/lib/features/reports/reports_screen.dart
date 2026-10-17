import 'dart:async';

import 'package:flutter/material.dart';

import '../../app/theme.dart';
import '../../core/api.dart';
import '../../core/auth_service.dart';
import '../../core/download.dart';
import '../../core/formatters.dart';
import '../../core/models.dart';
import '../../widgets/async_view.dart';
import '../../widgets/page_header.dart';
import '../../widgets/status_chip.dart';

enum _ReportType {
  summary('Summary Report'),
  detailed('Detailed Report'),
  executive('Executive Report');

  const _ReportType(this.label);
  final String label;
}

enum _ExportFormat { json, csv }

enum _ReportFormat {
  pdf('PDF'),
  docx('Word');

  const _ReportFormat(this.label);
  final String label;
}

/// Report sections and their API names.
const _sectionNames = {
  'Evidence Summary': 'evidence',
  'Timeline': 'timeline',
  'Findings & Conclusion': 'findings',
  'Recommendations': 'recommendations',
};

/// Generate a case report and list the case's reports.
///
/// API: POST /v1/cases/{caseId}/reports (17) starts generation in the
/// worker, GET .../reports (18) lists them, and GET .../download-url (19)
/// returns a signed URL to preview (PDF) or download the file. Reports are
/// PDF or Word. GET /v1/cases/{caseId}/export (50)
/// returns the findings as JSON or CSV for other systems.
class ReportsScreen extends StatefulWidget {
  const ReportsScreen({super.key, this.initialCaseId});

  final String? initialCaseId;

  @override
  State<ReportsScreen> createState() => _ReportsScreenState();
}

class _ReportsScreenState extends State<ReportsScreen> {
  final _api = PrufloApi();
  late Future<List<CaseItem>> _cases = _loadCases();
  String? _caseId;

  Future<List<CaseItem>> _loadCases() async {
    final (cases, _) = await _api.allCases(cap: 500);
    if (_caseId == null && cases.isNotEmpty) {
      final wanted = cases.where((c) =>
          c.id == widget.initialCaseId || c.reference == widget.initialCaseId);
      _caseId = (wanted.isNotEmpty ? wanted.first : cases.first).id;
    }
    return cases;
  }

  @override
  Widget build(BuildContext context) {
    final wide = MediaQuery.sizeOf(context).width >= 1100;
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          PageHeader(
            title: wide ? 'Generate Report' : 'Reports',
            subtitle: 'Create reports from the stored evidence and decisions',
          ),
          AsyncView<List<CaseItem>>(
            future: _cases,
            onRetry: () => setState(() { _cases = _loadCases(); }),
            builder: (context, cases) {
              final caseId = _caseId;
              if (cases.isEmpty || caseId == null) {
                return const SectionCard(
                  child: EmptyPanel(
                    icon: Icons.description_outlined,
                    title: 'No cases yet',
                    message: 'Reports are generated per case.',
                  ),
                );
              }
              return _CaseReports(
                key: ValueKey(caseId),
                cases: cases,
                caseId: caseId,
                wide: wide,
                onCaseChanged: (id) => setState(() => _caseId = id),
              );
            },
          ),
        ],
      ),
    );
  }
}

class _CaseReports extends StatefulWidget {
  const _CaseReports({
    super.key,
    required this.cases,
    required this.caseId,
    required this.wide,
    required this.onCaseChanged,
  });

  final List<CaseItem> cases;
  final String caseId;
  final bool wide;
  final ValueChanged<String> onCaseChanged;

  @override
  State<_CaseReports> createState() => _CaseReportsState();
}

class _CaseReportsState extends State<_CaseReports> {
  final _api = PrufloApi();
  _ReportType _type = _ReportType.detailed;
  _ReportFormat _format = _ReportFormat.pdf;
  final _sections = {for (final s in _sectionNames.keys) s: true};
  bool _generating = false;
  _ExportFormat _exportFormat = _ExportFormat.json;
  bool _exporting = false;

  List<ReportItem>? _reports;
  Object? _reportsError;
  Timer? _poll;

  CaseItem get _case => widget.cases.firstWhere((c) => c.id == widget.caseId);

  @override
  void initState() {
    super.initState();
    _loadReports();
  }

  @override
  void dispose() {
    _poll?.cancel();
    super.dispose();
  }

  Future<void> _loadReports() async {
    _poll?.cancel();
    try {
      final reports = await _api.reports(widget.caseId);
      if (!mounted) return;
      setState(() {
        _reports = reports;
        _reportsError = null;
      });
      // The worker makes queued reports ready shortly; check again soon.
      if (reports.any((r) => r.queued)) {
        _poll = Timer(const Duration(seconds: 3), _loadReports);
      }
    } catch (e) {
      if (mounted) setState(() => _reportsError = e);
    }
  }

  Future<void> _generate() async {
    setState(() => _generating = true);
    try {
      await _api.requestReport(
          widget.caseId,
          _type.name,
          [
            for (final e in _sections.entries)
              if (e.value) _sectionNames[e.key]!,
          ],
          format: _format.name);
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text('Generating the ${_format.label} report for Case '
            '#${_case.reference}. It will appear in the list when ready.'),
      ));
      await _loadReports();
    } catch (e) {
      if (mounted) showError(context, e);
    } finally {
      if (mounted) setState(() => _generating = false);
    }
  }

  Future<void> _open(ReportItem r, {required bool preview}) async {
    try {
      await openUrl(
          await _api.reportDownloadUrl(widget.caseId, r.id, preview: preview));
    } catch (e) {
      if (mounted) showError(context, e);
    }
  }

  Future<String?> _fetchExport() async {
    setState(() => _exporting = true);
    try {
      return await _api.exportFindings(widget.caseId, _exportFormat.name);
    } catch (e) {
      if (mounted) showError(context, e);
      return null;
    } finally {
      if (mounted) setState(() => _exporting = false);
    }
  }

  Future<void> _previewExport() async {
    final body = await _fetchExport();
    if (body == null || !mounted) return;
    final format = _exportFormat.name.toUpperCase();
    await showDialog<void>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text('Case #${_case.reference} export ($format)'),
        content: SizedBox(
          width: 560,
          height: 420,
          child: SingleChildScrollView(
            child: SelectableText(body,
                style: const TextStyle(fontFamily: 'monospace', fontSize: 12)),
          ),
        ),
        actions: [
          FilledButton(
            style: FilledButton.styleFrom(minimumSize: const Size(100, 44)),
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('Close'),
          ),
        ],
      ),
    );
  }

  Future<void> _downloadExport() async {
    final body = await _fetchExport();
    if (body == null || !mounted) return;
    final ext = _exportFormat.name;
    try {
      await saveTextFile('case-${_case.reference}-findings.$ext', body,
          ext == 'csv' ? 'text/csv' : 'application/json');
    } catch (e) {
      if (mounted) showError(context, e);
    }
  }

  @override
  Widget build(BuildContext context) {
    final canExport = AuthService.instance.user?.role.canExport ?? false;
    final anySection = _sections.values.any((v) => v);

    final caseField = DropdownButtonFormField<String>(
      initialValue: widget.caseId,
      isExpanded: true,
      items: [
        for (final c in widget.cases)
          DropdownMenuItem(
              value: c.id,
              child: Text('Case #${c.reference} - ${c.title}',
                  overflow: TextOverflow.ellipsis)),
      ],
      onChanged: (v) => widget.onCaseChanged(v!),
    );

    final form = SectionCard(
      title: 'Generate Report',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const _Label('Case'),
          caseField,
          const SizedBox(height: 20),
          if (!canExport)
            const Text(
              'Viewers can read reports but not generate them.',
              style: TextStyle(fontSize: 13, color: AppColors.textSecondary),
            )
          else ...[
            const _Label('Report Type'),
            for (final t in _ReportType.values)
              InkWell(
                borderRadius: BorderRadius.circular(8),
                onTap: () => setState(() => _type = t),
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 6),
                  child: Row(
                    children: [
                      Icon(
                        _type == t
                            ? Icons.radio_button_checked
                            : Icons.radio_button_off,
                        size: 20,
                        color: _type == t
                            ? AppColors.primary
                            : AppColors.textSecondary,
                      ),
                      const SizedBox(width: 10),
                      Text(t.label),
                    ],
                  ),
                ),
              ),
            const SizedBox(height: 16),
            const _Label('Format'),
            SegmentedButton<_ReportFormat>(
              segments: [
                for (final f in _ReportFormat.values)
                  ButtonSegment(
                    value: f,
                    label: Text(f.label),
                    icon: Icon(f == _ReportFormat.pdf
                        ? Icons.picture_as_pdf_outlined
                        : Icons.description_outlined),
                  ),
              ],
              selected: {_format},
              onSelectionChanged: (s) => setState(() => _format = s.first),
            ),
            const SizedBox(height: 16),
            const _Label('Include'),
            for (final entry in _sections.entries)
              CheckboxListTile(
                dense: true,
                contentPadding: EdgeInsets.zero,
                controlAffinity: ListTileControlAffinity.leading,
                value: entry.value,
                title: Text(entry.key),
                onChanged: (v) => setState(() => _sections[entry.key] = v!),
              ),
            const SizedBox(height: 16),
            FilledButton(
              onPressed: _generating || !anySection ? null : _generate,
              child: Text(_generating
                  ? 'Starting…'
                  : 'Generate ${_format.label} Report'),
            ),
          ],
        ],
      ),
    );

    final export = SectionCard(
      title: 'Export Findings Data',
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text(
            'Findings with their source references, for spreadsheets or other '
            'systems. Uses the case selected above.',
            style: TextStyle(fontSize: 13, color: AppColors.textSecondary),
          ),
          const SizedBox(height: 14),
          SegmentedButton<_ExportFormat>(
            segments: const [
              ButtonSegment(value: _ExportFormat.json, label: Text('JSON')),
              ButtonSegment(value: _ExportFormat.csv, label: Text('CSV')),
            ],
            selected: {_exportFormat},
            onSelectionChanged: (s) => setState(() => _exportFormat = s.first),
          ),
          const SizedBox(height: 14),
          Row(
            children: [
              Expanded(
                child: OutlinedButton(
                  onPressed: _exporting ? null : _previewExport,
                  child: const Text('Preview'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: FilledButton.icon(
                  onPressed: _exporting ? null : _downloadExport,
                  icon: const Icon(Icons.download_outlined, size: 18),
                  label: const Text('Download'),
                ),
              ),
            ],
          ),
        ],
      ),
    );

    final reports = _reports;
    final recent = SectionCard(
      title: 'Reports for Case #${_case.reference}',
      child: _reportsError != null
          ? ErrorPanel(
              message: '$_reportsError', onRetry: _loadReports, compact: true)
          : reports == null
              ? const Padding(
                  padding: EdgeInsets.all(16),
                  child: Center(
                      child: CircularProgressIndicator(strokeWidth: 2.5)),
                )
              : reports.isEmpty
                  ? const EmptyPanel(
                      icon: Icons.description_outlined,
                      title: 'No reports yet',
                      message: 'Generated reports for this case appear here.',
                    )
                  : Column(
                      children: [
                        for (final r in reports)
                          _ReportRow(
                            report: r,
                            onPreview: r.previewable
                                ? () => _open(r, preview: true)
                                : null,
                            onDownload: r.ready
                                ? () => _open(r, preview: false)
                                : null,
                          ),
                      ],
                    ),
    );

    if (widget.wide) {
      return Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            flex: 3,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                form,
                if (canExport) ...[const SizedBox(height: 20), export],
              ],
            ),
          ),
          const SizedBox(width: 20),
          Expanded(flex: 2, child: recent),
        ],
      );
    }
    // Phones lead with the report list, as in the mobile mockup.
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        recent,
        const SizedBox(height: 16),
        form,
        if (canExport) ...[const SizedBox(height: 16), export],
      ],
    );
  }
}

class _Label extends StatelessWidget {
  const _Label(this.text);
  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Text(text,
          style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w500)),
    );
  }
}

class _ReportRow extends StatelessWidget {
  const _ReportRow(
      {required this.report, required this.onPreview, required this.onDownload});
  final ReportItem report;
  final VoidCallback? onPreview;
  final VoidCallback? onDownload;

  @override
  Widget build(BuildContext context) {
    final status = switch (report.status) {
      'ready' => null,
      'failed' => const Pill('Failed',
          foreground: AppColors.danger, background: AppColors.dangerSoft),
      _ => const Pill('Generating',
          foreground: AppColors.warning, background: AppColors.warningSoft),
    };
    return InkWell(
      borderRadius: BorderRadius.circular(8),
      onTap: onPreview ?? onDownload,
      child: Padding(
        padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 4),
        child: Row(
          children: [
            Container(
              width: 36,
              height: 36,
              decoration: BoxDecoration(
                color:
                    report.isWord ? AppColors.primarySoft : AppColors.dangerSoft,
                borderRadius: BorderRadius.circular(8),
              ),
              child: Icon(
                  report.isWord
                      ? Icons.description_outlined
                      : Icons.picture_as_pdf_outlined,
                  size: 18,
                  color: report.isWord ? AppColors.primary : AppColors.danger),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(report.title,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          fontSize: 13, fontWeight: FontWeight.w600)),
                  Text(
                      [
                        report.formatLabel,
                        if (report.requestedBy != null)
                          report.requestedBy!.name,
                        timeAgo(report.createdAt),
                      ].join(' · '),
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                          fontSize: 12, color: AppColors.textSecondary)),
                ],
              ),
            ),
            ?status,
            if (onPreview != null)
              IconButton(
                tooltip: 'Preview',
                icon: const Icon(Icons.visibility_outlined, size: 18),
                onPressed: onPreview,
              ),
            IconButton(
              tooltip: 'Download ${report.formatLabel}',
              icon: const Icon(Icons.download_outlined, size: 18),
              onPressed: onDownload,
            ),
          ],
        ),
      ),
    );
  }
}
