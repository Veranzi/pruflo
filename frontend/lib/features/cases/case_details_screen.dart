import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../app/theme.dart';
import '../../core/api.dart';
import '../../core/api_client.dart';
import '../../core/auth_service.dart';
import '../../core/download.dart';
import '../../core/formatters.dart';
import '../../core/models.dart';
import '../../widgets/async_view.dart';
import '../../widgets/customer_picker.dart';
import '../../widgets/form_fields.dart';
import '../../widgets/status_chip.dart';
import 'case_members_section.dart';

/// One case: overview, its evidence, its timeline and the optional assistant
/// (GET /v1/cases/{id}, /evidence, /timeline; POST /qa).
class CaseDetailsScreen extends StatefulWidget {
  const CaseDetailsScreen({super.key, required this.caseId});

  final String caseId;

  @override
  State<CaseDetailsScreen> createState() => _CaseDetailsScreenState();
}

class _CaseDetailsScreenState extends State<CaseDetailsScreen> {
  final _api = PrufloApi();
  late Future<CaseItem> _future = _api.getCase(widget.caseId);

  void _reload() => setState(() {
        _future = _api.getCase(widget.caseId);
      });

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<CaseItem>(
      future: _future,
      builder: (context, snapshot) {
        final error = snapshot.error;
        if (error is ApiException && error.status == 404) {
          return Center(
            child: EmptyPanel(
              icon: Icons.search_off,
              title: 'Case not found',
              message: "It doesn't exist or you don't have access to it.",
              action: TextButton(
                onPressed: () => context.go('/cases'),
                child: const Text('Back to cases'),
              ),
            ),
          );
        }
        if (error != null) {
          return Center(child: ErrorPanel(message: '$error', onRetry: _reload));
        }
        final item = snapshot.data;
        if (item == null) {
          return const Center(child: CircularProgressIndicator(strokeWidth: 2.5));
        }
        return _CaseView(
          item: item,
          onChanged: (updated) =>
              setState(() {
            _future = Future.value(updated);
          }),
        );
      },
    );
  }
}

class _CaseView extends StatelessWidget {
  const _CaseView({required this.item, required this.onChanged});
  final CaseItem item;
  final ValueChanged<CaseItem> onChanged;

  @override
  Widget build(BuildContext context) {
    final canEdit = AuthService.instance.user?.role.canEditCases ?? false;
    return DefaultTabController(
      length: 4,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Container(
            color: Colors.white,
            padding: const EdgeInsets.fromLTRB(12, 12, 24, 0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    IconButton(
                      tooltip: 'Back to cases',
                      icon: const Icon(Icons.arrow_back),
                      onPressed: () => context.go('/cases'),
                    ),
                    Expanded(
                      child: Text('Case #${item.reference}',
                          style: const TextStyle(fontWeight: FontWeight.w600)),
                    ),
                    if (canEdit)
                      TextButton.icon(
                        onPressed: () async {
                          final updated = await showDialog<CaseItem>(
                            context: context,
                            builder: (_) => _EditCaseDialog(item),
                          );
                          if (updated != null) onChanged(updated);
                        },
                        icon: const Icon(Icons.edit_outlined, size: 16),
                        label: const Text('Edit'),
                      ),
                  ],
                ),
                Padding(
                  padding: const EdgeInsets.only(left: 12, top: 4),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Wrap(
                        spacing: 8,
                        children: [
                          StatusChip(item.status),
                          PriorityChip(item.priority),
                        ],
                      ),
                      const SizedBox(height: 10),
                      Text(item.title,
                          style: Theme.of(context)
                              .textTheme
                              .titleLarge
                              ?.copyWith(fontWeight: FontWeight.w700)),
                    ],
                  ),
                ),
                const SizedBox(height: 8),
                const TabBar(
                  isScrollable: true,
                  tabAlignment: TabAlignment.start,
                  tabs: [
                    Tab(text: 'Overview'),
                    Tab(text: 'Evidence'),
                    Tab(text: 'Timeline'),
                    Tab(text: 'Ask'),
                  ],
                ),
              ],
            ),
          ),
          const Divider(height: 1),
          Expanded(
            child: TabBarView(
              children: [
                _OverviewTab(item),
                _EvidenceTab(item),
                _TimelineTab(item),
                _AskTab(item),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _OverviewTab extends StatelessWidget {
  const _OverviewTab(this.item);
  final CaseItem item;

  @override
  Widget build(BuildContext context) {
    Widget field(String label, Widget value) => Padding(
          padding: const EdgeInsets.only(bottom: 18),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(label,
                  style: const TextStyle(
                      fontSize: 12, color: AppColors.textSecondary)),
              const SizedBox(height: 4),
              value,
            ],
          ),
        );
    const none = Text('—', style: TextStyle(color: AppColors.textSecondary));

    return SingleChildScrollView(
      padding: const EdgeInsets.all(24),
      child: Align(
        alignment: Alignment.topLeft,
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 640),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              field('Description',
                  item.description.isEmpty ? none : Text(item.description)),
              field('Category',
                  item.category.isEmpty ? none : Text(item.category)),
              field('Customer',
                  item.customerName == null ? none : Text(item.customerName!)),
              field('Created', Text(formatDateTime(item.createdAt))),
              field('Last updated', Text(formatDateTime(item.updatedAt))),
              field(
                'Assigned to',
                item.assignedTo == null
                    ? none
                    : Text(item.assignedTo!.name,
                        style: const TextStyle(fontWeight: FontWeight.w500)),
              ),
              field(
                'Priority',
                Align(
                    alignment: Alignment.centerLeft,
                    child: PriorityChip(item.priority)),
              ),
              const SizedBox(height: 8),
              FilledButton(
                onPressed: () =>
                    DefaultTabController.of(context).animateTo(1),
                child: const Text('View Evidence'),
              ),
              const SizedBox(height: 12),
              OutlinedButton(
                onPressed: () => context.go('/review?case=${item.id}'),
                child: const Text('Open Review Workspace'),
              ),
              // Who can see the case: admins manage it (endpoints 31, 32).
              if (AuthService.instance.isAdmin) ...[
                const SizedBox(height: 28),
                CaseMembersSection(caseId: item.id),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class _EvidenceTab extends StatefulWidget {
  const _EvidenceTab(this.item);
  final CaseItem item;

  @override
  State<_EvidenceTab> createState() => _EvidenceTabState();
}

class _EvidenceTabState extends State<_EvidenceTab> {
  final _api = PrufloApi();
  late Future<List<EvidenceItem>> _future = _api.evidence(widget.item.id);

  void _reload() => setState(() {
        _future = _api.evidence(widget.item.id);
      });

  Future<void> _view(EvidenceItem f) async {
    try {
      await openUrl(await _api.evidenceViewUrl(widget.item.id, f.id));
    } catch (e) {
      if (mounted) showError(context, e);
    }
  }

  Future<void> _retry(EvidenceItem f) async {
    try {
      await _api.retryEvidence(widget.item.id, f.id);
      _reload();
    } catch (e) {
      if (mounted) showError(context, e);
    }
  }

  @override
  Widget build(BuildContext context) {
    final canUpload = AuthService.instance.user?.role.canEditCases ?? false;
    return AsyncView<List<EvidenceItem>>(
      future: _future,
      onRetry: _reload,
      builder: (context, files) => RefreshIndicator(
        onRefresh: () async => _reload(),
        child: ListView(
          padding: const EdgeInsets.all(24),
          children: [
            if (files.isEmpty)
              const EmptyPanel(
                icon: Icons.upload_file_outlined,
                title: 'No evidence uploaded for this case yet.',
              ),
            for (final f in files)
              Card(
                margin: const EdgeInsets.only(bottom: 10),
                child: ListTile(
                  leading: Icon(
                    f.contentType.startsWith('image/')
                        ? Icons.image_outlined
                        : Icons.insert_drive_file_outlined,
                    color: AppColors.primary,
                  ),
                  title: Text(f.fileName, overflow: TextOverflow.ellipsis),
                  subtitle: Text(
                      '${formatBytes(f.sizeBytes)} · ${f.statusLabel}'
                      '${f.pageCount == null ? '' : ' · ${f.pageCount} pages'}'
                      ' · ${timeAgo(f.createdAt)}'),
                  trailing: Wrap(
                    children: [
                      if (canUpload && f.status == 'failed_terminal' ||
                          canUpload && f.status == 'failed_retryable')
                        IconButton(
                          tooltip: 'Retry processing',
                          icon: const Icon(Icons.refresh, size: 18),
                          onPressed: () => _retry(f),
                        ),
                      if (f.status != 'uploading')
                        IconButton(
                          tooltip: 'View file',
                          icon: const Icon(Icons.open_in_new, size: 18),
                          onPressed: () => _view(f),
                        ),
                    ],
                  ),
                ),
              ),
            if (canUpload) ...[
              const SizedBox(height: 12),
              Align(
                alignment: Alignment.centerLeft,
                child: OutlinedButton.icon(
                  style:
                      OutlinedButton.styleFrom(minimumSize: const Size(0, 44)),
                  onPressed: () => context.go('/evidence?case=${widget.item.id}'),
                  icon: const Icon(Icons.cloud_upload_outlined, size: 18),
                  label: const Text('Upload evidence'),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class _TimelineTab extends StatefulWidget {
  const _TimelineTab(this.item);
  final CaseItem item;

  @override
  State<_TimelineTab> createState() => _TimelineTabState();
}

class _TimelineTabState extends State<_TimelineTab> {
  final _api = PrufloApi();
  late Future<List<TimelineItem>> _future = _api.timeline(widget.item.id);

  void _reload() => setState(() {
        _future = _api.timeline(widget.item.id);
      });

  @override
  Widget build(BuildContext context) {
    return AsyncView<List<TimelineItem>>(
      future: _future,
      onRetry: _reload,
      builder: (context, events) => events.isEmpty
          ? const EmptyPanel(title: 'Nothing has happened yet.')
          : TimelineList(events, padding: const EdgeInsets.all(24)),
    );
  }
}

/// Newest-first list of timeline events (endpoint 16).
/// Endpoint 20: questions about the case, answered from its findings, claims
/// and timeline with citations. Machine-generated, read-only, optional.
class _AskTab extends StatefulWidget {
  const _AskTab(this.item);
  final CaseItem item;

  @override
  State<_AskTab> createState() => _AskTabState();
}

class _AskTabState extends State<_AskTab> {
  final _question = TextEditingController();
  final _api = PrufloApi();
  CaseAnswer? _answer;
  String? _error;
  bool _busy = false;

  @override
  void dispose() {
    _question.dispose();
    super.dispose();
  }

  Future<void> _ask() async {
    final question = _question.text.trim();
    if (question.length < 3 || _busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final answer = await _api.ask(widget.item.id, question);
      if (mounted) setState(() => _answer = answer);
    } on ApiException catch (e) {
      if (mounted) {
        setState(
          () => _error = e.code == 'assistant_unavailable'
              ? 'The case assistant isn\'t set up for this organisation.'
              : e.message,
        );
      }
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final answer = _answer;
    return SingleChildScrollView(
      padding: const EdgeInsets.all(24),
      child: Align(
        alignment: Alignment.topLeft,
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 640),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Text(
                'Ask about this case. Answers come only from its findings, the '
                'values read from its documents and its timeline, with references. '
                'They are machine-generated and never decide anything.',
                style: TextStyle(fontSize: 13, color: AppColors.textSecondary),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: _question,
                maxLength: 500,
                minLines: 1,
                maxLines: 3,
                textInputAction: TextInputAction.send,
                onSubmitted: (_) => _ask(),
                decoration: const InputDecoration(
                  hintText: 'e.g. Which items were delivered short?',
                ),
              ),
              Align(
                alignment: Alignment.centerRight,
                child: FilledButton(
                  onPressed: _busy ? null : _ask,
                  child: Text(_busy ? 'Asking…' : 'Ask'),
                ),
              ),
              if (_error != null) ...[
                const SizedBox(height: 12),
                Text(_error!, style: const TextStyle(color: AppColors.danger)),
              ],
              if (answer != null) ...[
                const SizedBox(height: 16),
                Card(
                  child: Padding(
                    padding: const EdgeInsets.all(16),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        SelectableText(answer.answer),
                        if (answer.citations.isNotEmpty) ...[
                          const SizedBox(height: 12),
                          Wrap(
                            spacing: 6,
                            runSpacing: 6,
                            children: [
                              for (final c in answer.citations)
                                ActionChip(
                                  label: Text(
                                    '${c.type == 'finding' ? 'Finding' : 'Value'} '
                                    '${c.id.substring(c.id.length - 6)}',
                                  ),
                                  onPressed: () => context.go(
                                    '/review?case=${widget.item.id}',
                                  ),
                                ),
                            ],
                          ),
                        ],
                        const SizedBox(height: 8),
                        const Text(
                          'Machine-generated',
                          style: TextStyle(
                            fontSize: 11,
                            color: AppColors.textSecondary,
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class TimelineList extends StatelessWidget {
  const TimelineList(this.events, {super.key, this.padding});
  final List<TimelineItem> events;
  final EdgeInsets? padding;

  @override
  Widget build(BuildContext context) {
    return ListView(
      padding: padding,
      children: [
        for (final e in events)
          ListTile(
            contentPadding: EdgeInsets.zero,
            leading:
                const Icon(Icons.circle, size: 10, color: AppColors.primary),
            minLeadingWidth: 16,
            title: Text(e.description, style: const TextStyle(fontSize: 14)),
            subtitle:
                Text(formatDateTime(e.at), style: const TextStyle(fontSize: 12)),
          ),
      ],
    );
  }
}

class _EditCaseDialog extends StatefulWidget {
  const _EditCaseDialog(this.item);
  final CaseItem item;

  @override
  State<_EditCaseDialog> createState() => _EditCaseDialogState();
}

class _EditCaseDialogState extends State<_EditCaseDialog> {
  final _formKey = GlobalKey<FormState>();
  late final _title = TextEditingController(text: widget.item.title);
  late final _category = TextEditingController(text: widget.item.category);
  late final _description =
      TextEditingController(text: widget.item.description);
  late Priority _priority = widget.item.priority;
  late String? _customerId = widget.item.customerId;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _title.dispose();
    _category.dispose();
    _description.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    if (!_formKey.currentState!.validate()) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final updated = await PrufloApi().updateCase(
        widget.item.id,
        {
          'title': _title.text.trim(),
          'category': _category.text.trim(),
          'description': _description.text.trim(),
          'priority': _priority.name,
          if (_customerId != widget.item.customerId) 'customer_id': _customerId,
        },
        version: widget.item.version,
      );
      if (mounted) Navigator.of(context).pop(updated);
    } catch (e) {
      if (mounted) setState(() => _error = '$e');
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: Text('Edit Case #${widget.item.reference}'),
      content: SizedBox(
        width: 440,
        child: Form(
          key: _formKey,
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                LabeledField(
                  label: 'Title',
                  controller: _title,
                  validator: (v) => Validators.required(v, 'Title'),
                ),
                const SizedBox(height: 14),
                LabeledField(label: 'Category', controller: _category),
                const SizedBox(height: 14),
                LabeledField(
                    label: 'Description',
                    controller: _description,
                    maxLines: 3),
                const SizedBox(height: 14),
                const Text('Priority',
                    style:
                        TextStyle(fontSize: 13, fontWeight: FontWeight.w500)),
                const SizedBox(height: 6),
                SegmentedButton<Priority>(
                  segments: const [
                    ButtonSegment(value: Priority.low, label: Text('Low')),
                    ButtonSegment(
                        value: Priority.medium, label: Text('Medium')),
                    ButtonSegment(value: Priority.high, label: Text('High')),
                  ],
                  selected: {_priority},
                  onSelectionChanged: (s) =>
                      setState(() => _priority = s.first),
                ),
                const SizedBox(height: 14),
                CustomerPicker(
                  value: _customerId,
                  enabled: !_busy,
                  onChanged: (v) => setState(() => _customerId = v),
                ),
                if (_error != null) ...[
                  const SizedBox(height: 12),
                  Text(_error!,
                      style: const TextStyle(
                          color: AppColors.danger, fontSize: 13)),
                ],
              ],
            ),
          ),
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('Cancel'),
        ),
        FilledButton(
          style: FilledButton.styleFrom(minimumSize: const Size(100, 44)),
          onPressed: _busy ? null : _save,
          child: Text(_busy ? 'Saving…' : 'Save'),
        ),
      ],
    );
  }
}
