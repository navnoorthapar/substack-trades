import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
SOURCE = (ROOT / 'build_site.py').read_text(encoding='utf-8')
NODE = shutil.which('node')


def javascript_between(start, end):
    start_index = SOURCE.index(start)
    end_index = SOURCE.index(end, start_index)
    return SOURCE[start_index:end_index]


def run_node(script):
    if not NODE:
        raise AssertionError('Node.js is required for client runtime tests')
    result = subprocess.run(
        [NODE, '--input-type=module', '--eval', script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        # A cold Node process can briefly contend with the release suite's
        # deterministic site builds on the scheduled Mac. Keep the subprocess
        # bounded without turning normal launch-gate load into a false failure.
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)


HARNESS_GLOBALS = r"""
const THREADS = {article_count:0,defaults:{},topics:{}};
const THREAD_ARTICLES = Object.create(null);
const ARTICLE_BY_ID = new Map();
const DESK_FACETS = {observation_count:0,source_note_count:0,outcome_count:0,
  instruments:[],underlyings:[],periods:[]};
const location = {href:'https://example.test/#view=structure'};
const RATE_CONTEXT = {
  schema_version:1, source:{name:'U.S. Treasury'},
  thresholds:{slope:[0.35,0.47],level:[4.36,4.49]},
  days:{'2026-01-05':['2026-01-05',4.10,4.40,4.90,'low','mid']}
};
const SNAPSHOT = {data_checksum:'runtime-test-checksum'};
const WORKFLOW_TEXT_LIMITS = {tags:500,note:4000,falsifier:1800};
const MAX_QUEUE_ITEMS = 250;
const PAGE_SIZE = {queue:100,structure:8};
"""

HARNESS_QUEUE = r"""
const workflowItems = new Map();
let savedIdeas = new Set();
let persisted = true;
let toast = '';
const state = {structureQuestion:'What published evidence should we verify?',
  structureFocus:'VIX',structureInstrument:'',structureDirection:'any',
  structurePeriod:'all',structureSlope:'any',structureLevel:'any',structureMacro:false,
  structureAnchor:'a_i1',structurePassage:'i1',structureShareable:false,limit:8,
  view:'structure',selected:''};
function showToast(value) { toast = value; }
function persistWorkflow() { return persisted; }
function confirmQueueStorageBoundary() { return true; }
function markMeaningfulNavigation() {}
function render() {}
function newWorkflowItem(id) {
  return {id:id,status:'review',note:'',falsifier:'',tags:'',thesis:'',updated_at:''};
}
function observation(id, overrides) {
  const idea = Object.assign({
    id:id, description:'passage ' + id, direction:'short', instruments:['option'],
    underlying:'VIX', thesis:'', quant:'2x', outcome:'', manager_raw:'',
    documentation_score:4,
    _article:{id:'a_' + id, title:'Note ' + id, date:'2026-01-05',
      url:'https://navnoorbawa.substack.com/p/note-' + id, source:'substack',
      body_revision_status:'current',content_status:'full'}
  }, overrides || {});
  idea._search = normalize([idea._article.title,idea.description,idea.direction,
    idea.instruments.join(' '),idea.underlying,idea.thesis,idea.quant,
    idea.outcome,idea.manager_raw].join(' '));
  return idea;
}
const IDEAS = [observation('i1'), observation('i2', {direction:'unspecified'})];
"""


class ClientErrorRuntimeTests(unittest.TestCase):
    def test_original_links_accept_only_canonical_owned_article_urls(self):
        function = javascript_between(
            'function safeUrl(value) {',
            '\nconst MONTHS =',
        )
        run_node(
            function
            + r'''
const allowed = [
  'https://navnoorbawa.substack.com/p/exact-research-note',
  'https://medium.com/@navnoorbawa/exact-research-note-abcdef123456',
  'https://medium.com/@navnoorbawa/r%C3%A9sum%C3%A9-abcdef123456'
];
for (const value of allowed) {
  if (safeUrl(value) !== value) throw new Error('owned article URL was rejected');
}
const rejected = [
  'http://medium.com/@navnoorbawa/note-abcdef123456',
  'https://user:secret@medium.com/@navnoorbawa/note-abcdef123456',
  'https://medium.com:443/@navnoorbawa/note-abcdef123456',
  'https://medium.com:444/@navnoorbawa/note-abcdef123456',
  'https://medium.com/@navnoorbawa/note-abcdef123456?source=feed',
  'https://medium.com/@navnoorbawa/note-abcdef123456#fragment',
  'https://medium.com/@another/note-abcdef123456',
  'https://navnoorbawa.medium.com/note-abcdef123456',
  'https://MEDIUM.com/@navnoorbawa/note-abcdef123456',
  'https://navnoorbawa.substack.com/archive',
  'https://navnoorbawa.substack.com/p/invalid.',
  'https://medium.com/@navnoorbawa/not-an-id',
  'https://medium.com/@navnoorbawa/r%c3%a9sum%c3%a9-abcdef123456',
  'https://medium.com/@navnoorbawa/folder/note-abcdef123456',
  'https://medium.com/@navnoorbawa/note%2fescape-abcdef123456'
];
for (const value of rejected) {
  if (safeUrl(value) !== '#') throw new Error('unsafe article URL was accepted: ' + value);
}
'''
        )

    def test_subscriber_conversion_is_source_exact_and_state_honest(self):
        functions = javascript_between(
            'function hasIndexedMemberPreview(article) {',
            '\nfunction articleEvidence(article)',
        )

        run_node(
            r'''
const SUBSCRIPTION_URL = 'https://www.navnoorbawaresearch.com/subscribe';
const escapeHtml = (value) => String(value);
const safeUrl = (value) => String(value);
const articleClaim = (article) => String(
  article && article.brief && article.brief.lead && article.brief.lead.text ||
  article && article.subtitle || article && article.title || ''
);
const bodyRevisionLabel = (article) => article.body_revision_status === 'prior'
  ? 'Prior revision capture' : article.body_revision_status === 'unverified'
    ? 'Revision unverified' : 'Current source body';
'''
            + functions
            + r'''
const exactUrl = 'https://navnoorbawa.substack.com/p/member-note';
const current = {
  id:'a_current',source:'substack',publication_access:'member',
  member_preview_chars:240,body_revision_status:'current',
  url:exactUrl,title:'Current member note',subtitle:'A bounded public framing.',
  brief:{lead:{text:'A bounded public framing.'}}
};
const currentMarkup = premiumAccessMarkup(current,'article');
if ((currentMarkup.match(/href=/g) || []).length !== 2) {
  throw new Error('conversion panel must expose exactly one note and one plans link');
}
if ((currentMarkup.match(new RegExp(exactUrl,'g')) || []).length !== 1 ||
    (currentMarkup.match(new RegExp(SUBSCRIPTION_URL,'g')) || []).length !== 1) {
  throw new Error('conversion destinations are duplicated or missing');
}
if (!currentMarkup.includes('public preview indexed') ||
    !currentMarkup.includes('Indexed public preview') ||
    !currentMarkup.includes('noopener noreferrer') ||
    !currentMarkup.includes('opens in a new tab')) {
  throw new Error('current-preview trust or accessibility copy is missing');
}

const prior = {...current,id:'a_prior',body_revision_status:'prior'};
const priorMarkup = premiumAccessMarkup(prior,'brief');
if (!priorMarkup.includes('stored public preview') ||
    !priorMarkup.includes('prior revision capture') ||
    priorMarkup.includes('premium-access-evidence')) {
  throw new Error('stored preview was presented as current');
}

const metadataOnly = {
  ...current,id:'a_metadata',member_preview_chars:0,subtitle:'',
  brief:{lead:null,sections:[],fallback_evidence:null,checkpoints:[]}
};
const metadataMarkup = premiumAccessMarkup(metadataOnly,'article');
if (!metadataMarkup.includes('metadata only') ||
    !metadataMarkup.includes('no anonymous body preview') ||
    !metadataMarkup.includes('Published metadata') ||
    metadataMarkup.includes('Indexed public preview')) {
  throw new Error('metadata-only member source was mislabeled as a captured preview');
}

const mediumLocked = {...current,id:'m_locked',source:'medium'};
const publicSubstack = {...current,id:'s_public',publication_access:'public'};
const unknownSubstack = {...current,id:'s_unknown',publication_access:'unknown'};
for (const article of [mediumLocked,publicSubstack,unknownSubstack]) {
  if (premiumAccessMarkup(article,'article') !== '') {
    throw new Error('Substack conversion panel escaped its exact paid-source boundary');
  }
}
'''
        )

    def test_copied_research_task_uses_its_retained_source_snapshot(self):
        function = javascript_between(
            'function decisionPacketText(idea,item) {',
            '\nfunction confirmQueueStorageBoundary()',
        )
        run_node(
            r'''
const DILIGENCE_GATES = [
  ['context_reviewed','Surrounding publication context reviewed'],
  ['public_source_recorded','Independent public source recorded']
];
const sourceLabel = (value) => value === 'medium' ? 'Medium' : 'Substack';
const workflowStatusLabel = () => 'Verifying';
const retainedReviewFlagSummary = () => 'Review flags unavailable at capture';
const sourceSnapshotForIdea = () => ({
  title:'CURRENT MUTATED TITLE',url:'https://navnoorbawa.substack.com/p/current',
  passage:'CURRENT MUTATED PASSAGE',source:'substack'
});
'''
            + function
            + r'''
const idea = {id:'i1'};
const item = {
  status:'diligence',priority:'high',owner:'Research',review_date:'2026-02-01',
  next_action:'Verify independently',thesis:'',contrary:'',catalyst:'',horizon:'',
  falsifier:'',independent_source:'BIS working paper, https://bis.org/example',
  numeric_source:'64.0%, cited table 2, annualized volatility',
  checks:{context_reviewed:true,public_source_recorded:false},
  check_times:{context_reviewed:'2026-01-01T01:02:03Z',public_source_recorded:''},
  tags:'',note:'',updated_at:'2026-01-01T00:00:00Z',
  source_snapshot:{
    title:'RETAINED TITLE',url:'https://medium.com/@navnoorbawa/retained-abcdef123456',
    passage:'RETAINED EXACT PASSAGE',source:'medium',date:'2025-12-01',
    data_checksum:'abcdef1234567890',publication_access:'public',
    body_revision_status:'prior',source_updated_at:'2025-12-02'
  }
};
const copied = decisionPacketText(idea,item);
for (const retained of ('RETAINED TITLE RETAINED EXACT PASSAGE ' +
    'https://medium.com/@navnoorbawa/retained-abcdef123456').split(' ')) {
  if (!copied.includes(retained)) throw new Error('retained provenance was lost: ' + retained);
}
if (copied.includes('CURRENT MUTATED')) {
  throw new Error('copied task silently rebound to mutable current-source data');
}
if (!copied.includes('LOCAL RESEARCH TASK') || copied.includes('Analyst confidence')) {
  throw new Error('copied artifact regressed to a scored decision packet');
}
for (const required of ['BIS working paper','64.0%','attested 2026-01-01T01:02:03Z','Review flags unavailable at capture']) {
  if (!copied.includes(required)) throw new Error('research-task evidence was lost: ' + required);
}
if (decisionPacketText(idea,{...item,source_snapshot:null}) !== '') {
  throw new Error('missing retained evidence silently fell back to current source data');
}
'''
        )

    def test_legacy_research_task_fields_are_removed_at_the_storage_boundary(self):
        function = javascript_between(
            'function normalizeWorkflowItem(value) {',
            '\nfunction newWorkflowItem',
        )
        run_node(
            r'''
const VALID_QUEUE_STATUSES = new Set(['review','diligence','monitor','archived']);
const VALID_PRIORITIES = new Set(['low','normal','high']);
const WORKFLOW_TEXT_LIMITS = {
  tags:500,note:4000,owner:120,horizon:160,thesis:1800,contrary:1600,
  catalyst:1400,independent_source:1200,numeric_source:1200,falsifier:1800,
  next_action:700
};
const DILIGENCE_GATES = [['context_reviewed','Source'],['public_source_recorded','Independent']];
const blankChecks = () => ({context_reviewed:false,public_source_recorded:false});
const blankCheckTimes = () => ({context_reviewed:'',public_source_recorded:''});
const normalizeSourceSnapshot = (_value,id) => ({article_id:id,url:'https://example.com/source'});
const validDateInput = (value) => String(value || '');
const validTimestamp = (value) => value === '2026-01-01T00:00:00Z' ? value : '';
'''
            + function
            + r'''
const item = normalizeWorkflowItem({
  id:'i_legacy',status:'diligence',priority:'high',confidence:'high',
  payoff:'PRIVATE ENTRY AND PAYOFF',implementation:'PRIVATE BORROW DETAIL',
  portfolio:'PRIVATE EXPOSURE DETAIL',risk:'PRIVATE EXIT DETAIL',
  thesis:'Public-source hypothesis',source_snapshot:{url:'https://example.com/source'},
  checks:{source:true,independent:true,market:true,liquidity:true,portfolio:true,compliance:true,
    context_reviewed:true,public_source_recorded:true,numeric_traced:true},
  check_times:{context_reviewed:'2026-01-01T00:00:00Z',numeric_traced:'not-a-timestamp'}
});
for (const forbidden of ['confidence','payoff','implementation','portfolio','risk']) {
  if (Object.prototype.hasOwnProperty.call(item,forbidden) ||
      JSON.stringify(item).includes('PRIVATE')) {
    throw new Error('legacy forbidden field survived normalization: ' + forbidden);
  }
}
for (const retiredGate of ['source','independent','market','liquidity','portfolio','compliance']) {
  if (Object.prototype.hasOwnProperty.call(item.checks,retiredGate)) {
    throw new Error('legacy gate silently became a new attestation: ' + retiredGate);
  }
}
if (item.thesis !== 'Public-source hypothesis' || !item.checks.context_reviewed) {
  throw new Error('supported research-task fields were lost during migration');
}
if (item.checks.public_source_recorded || item.checks.numeric_traced ||
    item.check_times.public_source_recorded || item.check_times.numeric_traced) {
  throw new Error('an un-timestamped or invalid attestation survived normalization');
}
if (normalizeWorkflowItem({
  id:'x" onmouseover="alert(1)',source_snapshot:{url:'https://example.com/source'}
}) !== null) {
  throw new Error('unsafe imported task ID survived normalization into markup attributes');
}
'''
        )

    def test_visible_task_provenance_uses_the_retained_snapshot_and_warns_on_drift(self):
        functions = javascript_between(
            'function retainedSourceSnapshotComparison(item,idea) {',
            '\nfunction blankChecks()',
        )
        run_node(
            r'''
const escapeHtml = (value) => String(value || '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const sourceLabel = (value) => value === 'medium' ? 'Medium' : 'Substack';
const safeUrl = (value) => String(value || '#');
const formatDate = (value) => value;
const retainedReviewFlagSummary = (snapshot) => snapshot.review_flags_verified ? 'No automated review flag captured' : 'Review flags unavailable at capture';
const observationsReady = true;
let current = {
  article_id:'a1',title:'CURRENT TITLE',url:'https://example.com/current',date:'2026-01-02',
  source:'substack',publication_access:'public',passage:'CURRENT MUTATED PASSAGE',
  direction:'long',instruments:['equity'],underlying:'Example',body_revision_status:'current',
  source_updated_at:'2026-01-02T00:00:00Z',observed_source_updated_at:'2026-01-02T00:00:00Z',
  data_checksum:'currentchecksum'
};
const sourceSnapshotForIdea = () => current;
'''
            + functions
            + r'''
const idea = {id:'i1'};
const item = {source_snapshot:{
  article_id:'a1',title:'RETAINED TITLE',url:'https://example.com/retained',date:'2025-12-01',
  source:'medium',publication_access:'public',passage:'RETAINED EXACT PASSAGE',
  direction:'short',instruments:['option'],underlying:'Retained',body_revision_status:'prior',
  source_updated_at:'2025-12-02T00:00:00Z',observed_source_updated_at:'2025-12-02T00:00:00Z',
  data_checksum:'retainedchecksum'
}};
const comparison = retainedSourceSnapshotComparison(item,idea);
if (!comparison.differences.includes('captured passage') ||
    !comparison.differences.includes('dataset revision')) {
  throw new Error('material retained-source drift was not detected');
}
const markup = retainedSourceSnapshotMarkup(item,idea);
for (const required of ['Retained task source snapshot','RETAINED TITLE','RETAINED EXACT PASSAGE',
    'The current release differs in','copied and exported tasks preserve it','noopener noreferrer']) {
  if (!markup.includes(required)) throw new Error('retained-source disclosure missing: ' + required);
}
if (markup.includes('CURRENT MUTATED PASSAGE')) {
  throw new Error('visible task provenance silently rebound to current source text');
}
current = {...item.source_snapshot};
const matching = retainedSourceSnapshotMarkup(item,idea);
if (!matching.includes('Retained evidence matches the current release record.')) {
  throw new Error('matching retained source was mislabeled as stale');
}
current = null;
const missingCurrent = retainedSourceSnapshotMarkup(item,idea);
if (!missingCurrent.includes('absent from the current release') ||
    !missingCurrent.includes('RETAINED EXACT PASSAGE')) {
  throw new Error('missing current observation hid or rebound the retained task');
}
'''
        )

    def test_missing_or_unsafe_task_snapshot_never_rebinds_to_current_source(self):
        function = javascript_between(
            'function normalizeSourceSnapshot(value,id) {',
            '\nfunction retainedSourceSnapshotComparison',
        )
        run_node(
            r'''
const VALID_SOURCES = new Set(['substack','medium']);
const VALID_PUBLICATION_ACCESS = new Set(['public','member','unknown']);
const VALID_DIRECTIONS = new Set(['long','short','arbitrage/relative value','long/short','unspecified']);
const safeUrl = (value) => String(value || '').startsWith('https://') ? String(value) : '#';
const validDateInput = (value) => /^\d{4}-\d{2}-\d{2}$/.test(String(value || '')) ? String(value) : '';
'''
            + function
            + r'''
for (const value of [null,{}, {url:'javascript:alert(1)'}]) {
  if (normalizeSourceSnapshot(value,'i_current') !== null) {
    throw new Error('missing or unsafe retained evidence silently rebound to current source');
  }
}
const valid = normalizeSourceSnapshot({
  article_id:'a1',title:'Retained',url:'https://example.com/source',date:'2026-01-01',
  source:'substack',publication_access:'public',passage:'Exact retained passage',
  direction:'long',instruments:['equity'],data_checksum:'abc',review_flags_verified:true,
  negation_risk:true,reference_line:false,description_truncated:true
},'i_current');
if (!valid || valid.passage !== 'Exact retained passage' || valid.url !== 'https://example.com/source' ||
    !valid.review_flags_verified || valid.negation_risk !== true || valid.reference_line !== false ||
    valid.description_truncated !== true) {
  throw new Error('valid retained evidence was rejected');
}
const legacy = normalizeSourceSnapshot({
  article_id:'a1',title:'Legacy retained',url:'https://example.com/legacy',date:'2025-01-01',
  source:'substack',publication_access:'public',passage:'Legacy passage',direction:'unspecified'
},'i_legacy');
if (!legacy || legacy.review_flags_verified || legacy.negation_risk !== null ||
    legacy.reference_line !== null || legacy.description_truncated !== null) {
  throw new Error('legacy snapshot without proof falsely asserted clean review flags');
}
const malformedFlags = normalizeSourceSnapshot({
  article_id:'a1',title:'Malformed flags',url:'https://example.com/malformed',date:'2025-01-01',
  source:'substack',publication_access:'public',passage:'Malformed flags passage',direction:'unspecified',
  review_flags_verified:true,negation_risk:false
},'i_malformed');
if (!malformedFlags || malformedFlags.review_flags_verified || malformedFlags.reference_line !== null) {
  throw new Error('partial review-flag proof falsely asserted a clean retained capture');
}
'''
        )

    def test_research_task_filter_sort_and_facets_use_retained_source_values(self):
        functions = (
            javascript_between(
                'function dateRangeAnchor() {',
                '\nfunction setMatches',
            )
            + javascript_between(
                'function ideaMatches(idea, skip) {',
                '\nfunction ideaMatchesResearchFacets',
            )
            + javascript_between(
                'function sortedRecords(records) {',
                '\nfunction filteredRecords',
            )
            + javascript_between(
                'function recordValues(record, facet) {',
                '\nfunction updateFacetCounts',
            )
            + javascript_between(
                'function queueRecordForWorkflow(item) {',
                '\nfunction reviewIsOverdue',
            )
        )
        run_node(
            r'''
const normalize = (value) => String(value || '').toLowerCase().replace(/\s+/g,' ').trim();
const state = {
  view:'queue',sort:'newest',query:'retained alpha',queueStatuses:new Set(),
  range:'all',
  sources:new Set(['medium']),revisions:new Set(['prior']),directions:new Set(['short']),
  instruments:new Set(['option']),publicationAccess:new Set(['public']),
  managers:new Set(),quality:new Set(),content:new Set(),documentation:'all',newOnly:false
};
const workflowStatusLabel = (value) => value;
const matchesSearch = (haystack) => normalize(state.query).split(' ').every((token) => haystack.includes(token));
const MAX_DATE = '2026-12-31';
const setMatches = (selected,values) => !selected.size || values.some((value) => selected.has(value));
const isArticleView = () => false;
const relevanceScore = () => 0;
const hasValue = (value) => Boolean(value);
const workflowItems = new Map();
const IDEA_BY_ID = new Map();
function idea(id,currentDate) {
  return {id,article_id:'current-' + id,direction:'long',instruments:['equity'],manager:'CURRENT MANAGER',
    manager_key:'current',documentation_score:5,quant:'CURRENT',thesis:'CURRENT',outcome:'CURRENT',
    _article:{id:'current-' + id,title:'CURRENT BETA',date:currentDate,source:'substack',
      publication_access:'member',body_revision_status:'current',content_status:'full'}};
}
const olderCurrent = idea('i_old','2026-12-31');
const newerCurrent = idea('i_new','2025-01-01');
IDEA_BY_ID.set('i_old',olderCurrent);
IDEA_BY_ID.set('i_new',newerCurrent);
workflowItems.set('i_old',{status:'review',priority:'normal',owner:'A',review_date:'',updated_at:'',
  next_action:'',thesis:'',contrary:'',independent_source:'',numeric_source:'',catalyst:'',horizon:'',
  falsifier:'',tags:'',note:'',source_snapshot:{article_id:'retained-old',title:'Retained Alpha old',
    passage:'retained alpha passage',underlying:'VIX',source:'medium',direction:'short',
    instruments:['option'],publication_access:'public',body_revision_status:'prior',date:'2024-01-01'}});
workflowItems.set('i_new',{status:'review',priority:'normal',owner:'B',review_date:'',updated_at:'',
  next_action:'',thesis:'',contrary:'',independent_source:'',numeric_source:'',catalyst:'',horizon:'',
  falsifier:'',tags:'',note:'',source_snapshot:{article_id:'retained-new',title:'Retained Alpha new',
    passage:'retained alpha passage',underlying:'VIX',source:'medium',direction:'short',
    instruments:['option'],publication_access:'public',body_revision_status:'prior',date:'2026-01-01'}});
workflowItems.set('i_orphan',{id:'i_orphan',status:'review',priority:'normal',owner:'C',review_date:'',updated_at:'',
  next_action:'',thesis:'',contrary:'',independent_source:'',numeric_source:'',catalyst:'',horizon:'',
  falsifier:'',tags:'',note:'',source_snapshot:{article_id:'retained-orphan',title:'Retained Alpha orphan',
    url:'https://example.com/orphan',passage:'retained alpha orphan passage',underlying:'VIX',source:'medium',direction:'short',
    instruments:['option'],publication_access:'public',body_revision_status:'prior',date:'2025-06-01',
    review_flags_verified:false,data_checksum:'old'}});
'''
            + functions
            + r'''
if (!ideaMatches(olderCurrent) || !ideaMatches(newerCurrent)) {
  throw new Error('retained task filters/search incorrectly used current source values');
}
state.query = 'current beta';
if (ideaMatches(olderCurrent) || ideaMatches(newerCurrent)) {
  throw new Error('current source text leaked into retained task search');
}
state.query = '';
const queueUniverse = queueRecords();
const orphan = queueUniverse.find((row) => row.id === 'i_orphan');
if (queueUniverse.length !== 3 || !orphan || !orphan._retainedOnly || !ideaMatches(orphan)) {
  throw new Error('orphaned retained task did not remain in the full queue workflow');
}
const ordered = sortedRecords([olderCurrent,newerCurrent]);
if (ordered[0].id !== 'i_new') throw new Error('queue newest sort used current publication dates');
state.range = '30d';
if (!inDateRange('2025-12-15')) throw new Error('queue date range used the mutable catalogue maximum');
state.range = 'all';
workflowItems.get('i_old').source_snapshot.date = '';
workflowItems.get('i_new').source_snapshot.date = '';
const emptyDateOrder = sortedRecords([olderCurrent,newerCurrent]);
if (emptyDateOrder.map((row) => row.id).join(',') !== 'i_new,i_old') {
  throw new Error('queue ties inherited mutable current-record order');
}
for (const [facet,expected] of [
  ['source','medium'],['revision','prior'],['access','public'],['direction','short'],['instrument','option']
]) {
  if (!recordValues(olderCurrent,facet).includes(expected)) {
    throw new Error('queue facet used current source for ' + facet);
  }
}
for (const unsupported of ['manager','quality','content']) {
  if (recordValues(olderCurrent,unsupported).length) {
    throw new Error('unsupported current-only queue facet remained active: ' + unsupported);
  }
}
'''
        )

    def test_empty_evidence_desk_defers_observations_until_first_setup(self):
        helpers = (
            javascript_between(
                'function normalize(value) {',
                'function articleBriefSearch',
            )
            + javascript_between(
                'function structureWords(value) {',
                'function structureInstrumentOptions()',
            )
            + javascript_between(
                'function currentStateNeedsObservations() {',
                '\nfunction queueObservationResultFocus',
            )
        )
        run_node(
            r'''
const state = {
  view:'structure',structureQuestion:'',structureFocus:'',structureInstrument:'',
  structureDirection:'any',structurePeriod:'all',directions:new Set(),
  instruments:new Set(),managers:new Set(),quality:new Set(),documentation:'all'
};
const isArticleView = () => false;
'''
            + helpers
            + r'''
let requests = 0;
function requestIfNeeded() {
  if (currentStateNeedsObservations()) requests += 1;
}
requestIfNeeded();
state.structureQuestion = 'Question wording must remain memory-only';
requestIfNeeded();
if (requests !== 0) throw new Error('empty/question-only landing requested observations');
state.structureFocus = 'VIX';
requestIfNeeded();
if (requests !== 1) throw new Error('first literal evidence subject did not request observations');
state.structureFocus = '';
state.structureInstrument = 'option';
if (!currentStateNeedsObservations()) {
  throw new Error('a structured evidence refinement did not define a setup');
}
'''
        )

    def test_compact_article_payload_hydrates_exact_runtime_fields(self):
        function = javascript_between(
            'function hydrateEmbeddedArticle(article) {',
            '\nconst THREADS =',
        )
        run_node(
            r'''
const ARTICLES = [
  {
    id:'instant',published_at:'2026-01-02T08:00:00Z',wordcount:550,
    idea_ids:['i_1','i_2'],_b:[63,3],_q:5
  },
  {id:'day',published_at:'2026-01-03',wordcount:330}
];
'''
            + function
            + r'''
const instant = ARTICLES[0];
if (instant.date !== '2026-01-02' || instant.publication_precision !== 'instant') {
  throw new Error('instant publication metadata was not restored');
}
if (instant.read_minutes !== 2 || instant.trade_count !== 2) {
  throw new Error('Python-compatible read time or observation count changed');
}
if (!instant.brief_features.lead || !instant.brief_features.mechanism ||
    instant.brief_features.checkpoint_count !== 3) {
  throw new Error('brief feature mask was not restored');
}
if (!instant.has_quant || instant.has_thesis || !instant.has_outcome) {
  throw new Error('coverage feature mask was not restored');
}
if ('_b' in instant || '_q' in instant) throw new Error('wire fields leaked into runtime state');

const day = ARTICLES[1];
if (day.date !== '2026-01-03' || day.publication_precision !== 'day' ||
    day.read_minutes !== 2 || day.brief !== null ||
    day.idea_ids.length || day.managers.length) {
  throw new Error('omitted article defaults were not restored');
}
'''
        )

    def test_thread_hashes_accept_only_owned_topics_and_canonicalize_invalid_input(self):
        functions = javascript_between(
            'function hydrateFromHash() {',
            '\nlet queryCacheKey',
        )
        run_node(
            r'''
const PAGE_SIZE = {briefing:24,ideas:50,research:80,queue:100,structure:8};
const THREAD_ARTICLES = {
  a_current:{topics:['vix','options'],default_topic:'vix'}
};
const ARTICLE_BY_ID = new Map([['a_current',{id:'a_current'}]]);
const MANAGERS = [];
const VALID_SOURCES = new Set();
const VALID_BODY_REVISIONS = new Set(['current','prior','unverified']);
const VALID_DIRECTIONS = new Set(['long','short']);
const VALID_INSTRUMENTS = new Set(['option','equity']);
const VALID_QUALITY = new Set();
const VALID_PUBLICATION_ACCESS = new Set(['public','member','unknown']);
const VALID_CONTENT = new Set();
const VALID_QUEUE_STATUSES = new Set();
const VALID_DOCUMENTATION = new Set(['all']);
const VALID_BRIEF_LENSES = new Set(['all']);
const VALID_RATE_BANDS = new Set(['low','mid','high']);
const storedDensity = 'compact';
const setFromParam = () => new Set();
const state = {
  view:'briefing',query:'',sources:new Set(),revisions:new Set(),directions:new Set(),
  instruments:new Set(),managers:new Set(),quality:new Set(),publicationAccess:new Set(),content:new Set(),
  queueStatuses:new Set(),documentation:'all',newOnly:false,range:'all',
  coverage:'all',briefLens:'all',threadTopic:'',sort:'newest',
  structureQuestion:'',structureFocus:'',structureInstrument:'',structureDirection:'any',
  structurePeriod:'all',structureSlope:'any',structureLevel:'any',
  structureMacro:false,structureAnchor:'',structurePassage:'',
  structureShareable:false,structureControlsOpen:false,
  density:'compact',selected:'',limit:24
};
const historyCalls = [];
globalThis.history = {
  pushState(_state,_title,target) { historyCalls.push(['push',target]); },
  replaceState(_state,_title,target) { historyCalls.push(['replace',target]); }
};
globalThis.location = {
  href:'https://example.test/terminal/#view=briefing&selected=a_current&topic=vix',
  hash:'#view=briefing&selected=a_current&topic=vix',pathname:'/terminal/',search:''
};
'''
            + functions
            + r'''
hydrateFromHash();
if (state.threadTopic !== 'vix') throw new Error('owned thread topic was rejected');

location.hash = '#view=briefing&selected=a_current&topic=unowned';
hydrateFromHash();
if (state.threadTopic !== '') throw new Error('unowned thread topic was accepted');
updateHash();
if (historyCalls.length !== 1 || historyCalls[0][0] !== 'replace' ||
    historyCalls[0][1] !== '#view=briefing&selected=a_current' ||
    historyCalls[0][1].includes('topic=')) {
  throw new Error('invalid topic hash was not canonicalized safely');
}

location.hash = '#view=ideas&selected=a_current&topic=vix';
hydrateFromHash();
if (state.threadTopic !== '') throw new Error('topic escaped the briefing view');

// Article links created before Structure Desk became the default still open
// the exact dossier and are canonicalized to the explicit briefing route.
historyCalls.length = 0;
location.hash = '#selected=a_current';
hydrateFromHash();
if (state.view !== 'briefing' || state.selected !== 'a_current') {
  throw new Error('legacy article deep link was diverted to Structure Desk');
}
updateHash();
if (historyCalls.length !== 1 ||
    historyCalls[0][1] !== '#view=briefing&selected=a_current') {
  throw new Error('legacy article deep link was not canonicalized');
}

// A deliberately shared Structure URL restores only bounded values. The
// selected passage is meaningful only together with a real source note.
historyCalls.length = 0;
location.hash = '#squestion=Could+this+evidence+survive+review%3F&focus=VIX' +
  '&sinst=option&sdir=long&speriod=2026&smacro=1' +
  '&sslope=high&slevel=low&sanchor=a_current&spassage=i_passage';
hydrateFromHash();
if (state.view !== 'structure' ||
    state.structureQuestion !== 'Could this evidence survive review?' ||
    state.structureFocus !== 'VIX' ||
    state.structureInstrument !== 'option' || state.structureDirection !== 'long' ||
    state.structurePeriod !== '2026' || !state.structureMacro ||
    state.structureSlope !== 'any' || state.structureLevel !== 'any' ||
    state.structureAnchor !== 'a_current' || state.structurePassage !== 'i_passage' ||
    !state.structureShareable || !state.structureControlsOpen) {
  throw new Error('a bounded shared Structure view was not restored');
}

// Macro bands never become active merely because a crafted URL names them,
// and an unowned note can never authorize a passage anchor.
location.hash = '#focus=FX&sslope=high&slevel=low&sanchor=unknown&spassage=i_bad';
hydrateFromHash();
if (state.structureMacro || state.structureSlope !== 'any' ||
    state.structureLevel !== 'any') {
  throw new Error('macro bands escaped their explicit opt-in');
}
if (state.structureAnchor || state.structurePassage) {
  throw new Error('an unowned source passage anchor was accepted');
}
historyCalls.length = 0;
updateHash();
if (historyCalls.length !== 1 || historyCalls[0][1] !== '#focus=FX') {
  throw new Error('invalid Structure values were not canonicalized');
}

// Ordinary edits are private to memory. Canonicalizing an unshared Structure
// setup clears a stale hash all the way back to the path—never a dangling #.
state.structureFocus = 'private setup';
state.structureQuestion = 'private research question';
state.structureInstrument = '';
state.structureDirection = 'any';
state.structurePeriod = 'all';
state.structureMacro = false;
state.structureSlope = 'any';
state.structureLevel = 'any';
state.structureAnchor = '';
state.structurePassage = '';
state.structureShareable = false;
location.hash = '#focus=old';
historyCalls.length = 0;
updateHash();
if (historyCalls.length !== 1 || historyCalls[0][0] !== 'replace' ||
    historyCalls[0][1] !== '/terminal/') {
  throw new Error('an ordinary Structure edit leaked into the address');
}

// Copy view asks for a pure URL. It must not mutate history or the current
// address while serializing the explicitly shared, non-confidential setup.
location.hash = '';
historyCalls.length = 0;
const shareUrl = updateHash(true,true);
if (historyCalls.length !== 0 ||
    !shareUrl.includes('#squestion=private+research+question&focus=private+setup')) {
  throw new Error('Copy view mutated history or failed to serialize its bounded setup');
}

// An already-empty canonical address is stable and causes no history churn.
state.structureFocus = '';
state.structureQuestion = '';
state.structureShareable = false;
location.hash = '';
historyCalls.length = 0;
updateHash();
if (historyCalls.length !== 0) {
  throw new Error('the empty Structure address was rewritten unnecessarily');
}
'''
        )

    def test_deferred_thread_comparison_ignores_stale_context_and_reports_outcomes(self):
        branch = javascript_between(
            "  const threadLoad = event.target.closest('[data-thread-load]');",
            "  const threadArticle = event.target.closest('[data-thread-article]');",
        )
        run_node(
            r'''
const prior = {id:'a_prior'};
const ARTICLE_BY_ID = new Map([['a_prior',prior]]);
const state = {view:'briefing',selected:'a_current',threadTopic:'vix'};
let threadComparisonRequest = null;
let pendingBriefFocus = null;
let renderCount = 0;
let resolver = null;
const messages = [];
const announcer = {textContent:''};
const document = {getElementById(id) {
  if (id !== 'announcer') throw new Error('unexpected document lookup');
  return announcer;
}};
const render = () => { renderCount += 1; };
const showToast = (message) => messages.push(message);
const ensureArticleBrief = () => new Promise((resolve) => { resolver = resolve; });
const eventForPrior = {
  target:{closest(selector) {
    return selector === '[data-thread-load]' ? {dataset:{threadLoad:'a_prior'}} : null;
  }}
};
function handleThreadLoad(event) {
'''
            + branch
            + r'''
}

handleThreadLoad(eventForPrior);
if (!threadComparisonRequest || renderCount !== 1 ||
    pendingBriefFocus.kind !== 'thread-comparison' ||
    !announcer.textContent.startsWith('Loading and verifying')) {
  throw new Error('comparison did not enter an announced loading state');
}
resolver({lead:{text:'exact prior passage'}});
await Promise.resolve();
if (threadComparisonRequest !== null || renderCount !== 2 ||
    announcer.textContent !== 'Exact prior passage comparison loaded and verified') {
  throw new Error('verified comparison did not complete in the current context');
}

handleThreadLoad(eventForPrior);
resolver(null);
await Promise.resolve();
if (renderCount !== 4 ||
    announcer.textContent !== 'Exact prior dossier could not be verified; retry is available' ||
    messages.at(-1) !== 'Exact prior dossier could not be verified') {
  throw new Error('failed comparison did not expose a bounded retry state');
}

handleThreadLoad(eventForPrior);
const beforeStaleCompletion = renderCount;
state.selected = 'a_other';
resolver({lead:{text:'stale prior passage'}});
await Promise.resolve();
if (renderCount !== beforeStaleCompletion ||
    announcer.textContent !== 'Loading and verifying the exact prior article dossier') {
  throw new Error('stale comparison completion changed the active context');
}
'''
        )

    def test_thread_timeline_navigation_requires_exact_topic_membership(self):
        branch = javascript_between(
            "  const threadArticle = event.target.closest('[data-thread-article]');",
            "  const briefLens = event.target.closest('[data-brief-lens]');",
        )
        run_node(
            r'''
const THREADS = {
  topics:{vix:{article_ids:['a_prior','a_current']}}
};
const state = {
  view:'briefing',briefLens:'evidence',selected:'a_current',threadTopic:'vix'
};
let pendingBriefFocus = null;
let navigationMarked = false;
let renderCount = 0;
const shell = {scrollTop:900};
const markMeaningfulNavigation = () => { navigationMarked = true; };
const render = () => { renderCount += 1; };
const document = {getElementById(id) {
  if (id !== 'briefing-shell') throw new Error('unexpected document lookup');
  return shell;
}};
function eventFor(articleId) {
  return {target:{closest(selector) {
    return selector === '[data-thread-article]'
      ? {dataset:{threadArticle:articleId}} : null;
  }}};
}
function handleThreadArticle(event) {
'''
            + branch
            + r'''
}

handleThreadArticle(eventFor('a_unowned'));
if (state.selected !== 'a_current' || renderCount || navigationMarked) {
  throw new Error('unowned timeline article changed the dossier');
}

handleThreadArticle(eventFor('a_prior'));
if (state.selected !== 'a_prior' || state.view !== 'briefing' ||
    state.briefLens !== 'all' || state.threadTopic !== 'vix' ||
    pendingBriefFocus.kind !== 'article' ||
    pendingBriefFocus.value !== 'a_prior' || renderCount !== 1 ||
    !navigationMarked || shell.scrollTop !== 0) {
  throw new Error('owned timeline article did not preserve its thread context');
}
'''
        )

    def test_malformed_and_oversized_queue_imports_fail_closed(self):
        function = javascript_between(
            'function restoreQueueFile(file) {',
            "\n\ndocument.getElementById('table-body')",
        )
        run_node(
            function
            + r'''
const messages = [];
let readerCount = 0;
globalThis.showToast = (message) => messages.push(message);
globalThis.confirmQueueStorageBoundary = () => true;
globalThis.FileReader = class {
  constructor() { readerCount += 1; this.result = ''; }
  readAsText(file) { this.result = file.text; this.onload(); }
};
globalThis.cloneWorkflowMap = (value) => new Map(value);
globalThis.workflowItems = new Map();
globalThis.MAX_QUEUE_ITEMS = 250;
globalThis.normalizeWorkflowItem = () => null;
globalThis.SNAPSHOT = {data_checksum:'release'};
globalThis.window = {confirm:() => true};
globalThis.sessionStorage = {setItem() {}};
globalThis.RESTORE_ROLLBACK_KEY = 'rollback';
globalThis.persistWorkflow = () => true;
globalThis.render = () => {};
globalThis.showPersistentNotice = () => {};
globalThis.number = String;
globalThis.savedIdeas = new Set();
globalThis.lastRestoreWorkflowItems = null;

restoreQueueFile({size:2000001,text:'{}'});
if (readerCount !== 0 || messages.at(-1) !== 'Queue backup is missing or too large') {
  throw new Error('oversized queue import did not fail before reading');
}
restoreQueueFile({size:12,text:'{"broken"'});
if (readerCount !== 1 || messages.at(-1) !== 'Queue backup could not be validated') {
  throw new Error('malformed queue import was not rejected safely');
}
'''
        )

    def test_unavailable_session_storage_preserves_in_memory_queue(self):
        function = javascript_between(
            'function persistWorkflow() {',
            '\n\nARTICLES.forEach',
        )
        run_node(
            function
            + r'''
const messages = [];
globalThis.workflowItems = new Map([['i_test',{id:'i_test'}]]);
globalThis.savedIdeas = new Set();
globalThis.workflowSerialization = () => '[{"id":"i_test"}]';
globalThis.workflowLoadBlocked = false;
globalThis.workflowStorageDirty = false;
globalThis.workflowStorageUnavailable = false;
globalThis.lastPersistedWorkflow = '';
globalThis.legacyCleanupPending = false;
globalThis.WORKFLOW_KEY = 'queue';
globalThis.sessionStorage = {setItem() { throw new Error('storage blocked'); }};
globalThis.syncWorkflowStorageAlert = () => {};
globalThis.showToast = (message) => messages.push(message);

if (persistWorkflow() !== false) throw new Error('storage failure was accepted');
if (!workflowStorageDirty || !workflowStorageUnavailable) {
  throw new Error('storage failure did not retain dirty in-memory state');
}
if (!workflowItems.has('i_test') || messages.at(-1) !== 'Queue could not be saved in this tab session') {
  throw new Error('storage failure lost the queue or its recovery message');
}
'''
        )

    def test_queue_import_requires_explicit_retained_source_conflict_approval(self):
        function = javascript_between(
            'function restoreQueueFile(file) {',
            "\n\ndocument.getElementById('table-body')",
        )
        run_node(
            function
            + r'''
const messages = [];
const confirmations = [];
globalThis.showToast = (message) => messages.push(message);
globalThis.confirmQueueStorageBoundary = () => true;
globalThis.FileReader = class {
  readAsText(file) { this.result = file.text; this.onload(); }
};
const existing = {id:'i_same',updated_at:'2026-01-01T00:00:00Z',source_snapshot:{url:'https://example.com/retained-a',passage:'A'}};
globalThis.workflowItems = new Map([['i_same',existing]]);
globalThis.cloneWorkflowMap = (value) => new Map(Array.from(value.entries()).map(([key,item]) => [key,JSON.parse(JSON.stringify(item))]));
globalThis.MAX_QUEUE_ITEMS = 250;
globalThis.normalizeWorkflowItem = (value) => value;
globalThis.SNAPSHOT = {data_checksum:'release'};
globalThis.window = {confirm(message) {
  confirmations.push(message);
  return !message.startsWith('Retained source conflicts');
}};
globalThis.sessionStorage = {setItem() { throw new Error('rollback must not be written before conflict approval'); }};
globalThis.RESTORE_ROLLBACK_KEY = 'rollback';
globalThis.persistWorkflow = () => true;
globalThis.render = () => {};
globalThis.showPersistentNotice = () => {};
globalThis.number = String;
globalThis.savedIdeas = new Set(['i_same']);
globalThis.lastRestoreWorkflowItems = null;
const imported = {id:'i_same',updated_at:'2026-02-01T00:00:00Z',source_snapshot:{url:'https://example.com/retained-b',passage:'B'}};
restoreQueueFile({size:500,text:JSON.stringify({schema_version:3,data_checksum:'release',items:[imported]})});
if (workflowItems.get('i_same').source_snapshot.passage !== 'A') {
  throw new Error('conflicting import silently replaced the retained source anchor');
}
if (!confirmations.some((message) => message.startsWith('Retained source conflicts')) ||
    messages.at(-1) !== 'Queue import cancelled because retained source anchors conflict') {
  throw new Error('retained-source conflict was not separately disclosed and rejected');
}
'''
        )

    def test_deferred_network_failure_and_timeout_use_safe_messages(self):
        function = javascript_between(
            'function fetchReleaseText(url,unavailableMessage) {',
            '\nfunction loadBriefArchive()',
        )
        run_node(
            function
            + r'''
globalThis.AbortController = class { constructor() { this.signal = {}; } abort() {} };
globalThis.fetch = () => Promise.resolve({ok:false,text:() => Promise.resolve('unsafe')});
let first = '';
try { await fetchReleaseText('/missing.json','Observation archive is unavailable'); }
catch (error) { first = error.message; }
if (first !== 'Observation archive is unavailable') throw new Error('HTTP failure message changed');

globalThis.fetch = () => Promise.reject(Object.assign(new Error('aborted'),{name:'AbortError'}));
let second = '';
try { await fetchReleaseText('/slow.json','Observation archive is unavailable'); }
catch (error) { second = error.message; }
if (second !== 'Observation archive is unavailable (request timed out)') {
  throw new Error('timeout did not produce a safe recovery message');
}
'''
        )

    def test_stale_shell_recovery_is_bounded_to_one_release_reload(self):
        function = javascript_between(
            'function recoverFromStaleReleaseShell() {',
            '\nfunction fetchReleaseText',
        )
        run_node(
            function
            + r'''
globalThis.SNAPSHOT = {data_checksum:'abcdef0123456789fedcba'};
let replaced = '';
globalThis.window = {location:{
  href:'https://example.test/research/#view=ideas',
  replace(value) { replaced = value; }
}};
if (!recoverFromStaleReleaseShell()) throw new Error('stale shell did not request recovery');
if (!replaced.includes('nrt_release=abcdef0123456789')) throw new Error('release token was not bounded');
window.location.href = replaced;
if (recoverFromStaleReleaseShell()) throw new Error('recovery would reload the same release repeatedly');
'''
        )


class StructureDeskRuntimeTests(unittest.TestCase):
    """The desk retrieves bounded research analogues without inventing trades.

    A hard input excludes rather than pads; source notes—not repeated passages—
    are the unit of coverage; loose text mentions never enter the direct set.
    """

    def desk_runtime(self, assertions):
        run_node(
            javascript_between(
                'function normalize(value) {',
                'function articleBriefSearch',
            )
            + javascript_between(
                'function instrumentLabel(value) {',
                'function isArticleView()',
            )
            + javascript_between(
                'function publicationAccessLabel(article) {',
                'function sourceAccessLabel(article) {',
            )
            + javascript_between(
                'function reviewFlagged(idea) {',
                'function validDateInput',
            )
            + r'''
const THREADS = {article_count:0,defaults:{},topics:{}};
const THREAD_ARTICLES = Object.create(null);
const ARTICLE_BY_ID = new Map();
const DESK_FACETS = {observation_count:0,source_note_count:0,outcome_count:0,
  instruments:[],underlyings:[],periods:[]};
const location = {href:'https://example.test/#view=structure'};
const SNAPSHOT = {data_checksum:'runtime-test-checksum'};
const RATE_CONTEXT = {
  schema_version:1,
  source:{name:'U.S. Treasury Daily Treasury Par Yield Curve Rates'},
  thresholds:{slope:[0.35,0.47],level:[4.36,4.49]},
  days:{
    '2026-01-05':['2026-01-05',4.10,4.40,4.90,'low','mid'],
    '2026-02-05':['2026-02-02',4.00,4.60,5.00,'high','high']
  }
};
'''
            + javascript_between(
                'function underlyingParts(idea) {',
                'function structureChipRow(',
            )
            + javascript_between(
                'function structurePassageWarnings(idea) {',
                'function structurePassageMarkup(',
            )
            + javascript_between(
                'function countLabel(value,singular,plural) {',
                'function structureRelatedPanel(rows) {',
            )
            + javascript_between(
                'function deskShare(',
                'function renderStructureDesk(',
            )
            + r'''
function observation(overrides) {
  const idea = Object.assign({
    id:'i_0', description:'', direction:'unspecified', instruments:['equity'],
    underlying:'', thesis:'', quant:'', outcome:'', manager_raw:'',
    documentation_score:0,description_truncated:false,reference_line:false,
    negation_risk:false,_article:null
  }, overrides || {});
  idea._article = Object.assign({
    id:'a_' + idea.id,title:'Note ' + idea.id,date:'2026-01-01',
    url:'https://navnoorbawa.substack.com/p/note-' + idea.id,source:'substack',
    body_revision_status:'current',content_status:'full'
  }, idea._article || {});
  idea._search = normalize([
    idea._article.title, idea.description, idea.direction,
    idea.instruments.join(' '), idea.underlying, idea.thesis, idea.quant,
    idea.outcome, idea.manager_raw
  ].join(' '));
  return idea;
}
function deskSets(patch) {
  Object.assign(state, {
    structureQuestion:'', structureFocus:'', structureInstrument:'', structureDirection:'any',
    structurePeriod:'all', structureSlope:'any', structureLevel:'any',
    structureMacro:false, structureAnchor:'', structurePassage:''
  }, patch || {});
  return structureMatchSets();
}
function desk(patch) { return deskSets(patch).primary; }
const state = {structureQuestion:'',structureFocus:'',structureInstrument:'',structureDirection:'any',
  structurePeriod:'all',structureSlope:'any',structureLevel:'any',
  structureMacro:false,structureAnchor:'',structurePassage:''};
'''
            + assertions,
        )

    def test_hard_desk_inputs_exclude_rather_than_pad_the_comparison(self):
        self.desk_runtime(r'''
const IDEAS = [
  observation({id:'a', instruments:['option'], direction:'short'}),
  observation({id:'b', instruments:['equity'], direction:'short'}),
  observation({id:'c', instruments:['option'], direction:'long'})
];
if (desk({}).length !== 0) {
  throw new Error('an undefined setup silently expanded to the whole archive');
}
const byInstrument = desk({structureInstrument:'option'}).map(function (row) { return row.idea.id; });
if (byInstrument.length !== 2 || byInstrument.indexOf('b') !== -1) {
  throw new Error('instrument input did not exclude the other instrument: ' + byInstrument);
}
const byStance = desk({structureInstrument:'option', structureDirection:'short'});
if (byStance.length !== 1 || byStance[0].idea.id !== 'a') {
  throw new Error('stance input did not narrow to the matching passage');
}
if (byStance[0].reasons.length !== 2) {
  throw new Error('every applied input must be reported as a match reason');
}
const unmatched = desk({structureFocus:'nothingmatchesthisword'});
if (unmatched.length !== 0) throw new Error('an unmatched focus must return nothing');
''')

    def test_retrieval_tiers_never_promote_loose_mentions(self):
        self.desk_runtime(r'''
const IDEAS = [
  observation({id:'exact', underlying:'VIX', quant:'19 handle'}),
  observation({id:'subject', _article:{title:'VIX Alpha outlook'}}),
  observation({id:'structured', manager_raw:'VIX Alpha Optiver'}),
  observation({id:'mention', description:'VIX Alpha Optiver Volga appear only in prose.'}),
  observation({id:'unrelated', description:'Completely separate research.'})
];

const exact = deskSets({structureFocus:'VIX'});
if (exact.tier !== 'exact' || exact.primary.map(function (row) {
  return row.idea.id;
}).join(',') !== 'exact') {
  throw new Error('direct underlying evidence did not remain the primary tier');
}
if (exact.related.map(function (row) { return row.idea.id; }).join(',') !==
    'subject,structured,mention') {
  throw new Error('lower tiers were not separated beneath exact evidence');
}
if (!exact.primary[0].reasons.join(' ').includes('Same underlying')) {
  throw new Error('the direct-underlying reason was lost');
}

const subject = deskSets({structureFocus:'Alpha'});
if (subject.tier !== 'subject' || subject.primary.length !== 1 ||
    subject.primary[0].idea.id !== 'subject') {
  throw new Error('an authored headline subject was not isolated');
}
if (!subject.primary[0].reasons.join(' ').includes('Authored headline subject')) {
  throw new Error('the headline-subject reason was lost');
}

const structured = deskSets({structureFocus:'Optiver'});
if (structured.tier !== 'related' || structured.primary.length !== 1 ||
    structured.primary[0].idea.id !== 'structured') {
  throw new Error('a structured-field match was not isolated');
}
if (structured.related.length !== 1 || structured.related[0].idea.id !== 'mention') {
  throw new Error('a prose mention entered the structured-field set');
}

const mention = deskSets({structureFocus:'Volga'});
if (mention.tier !== 'none' || mention.primary.length !== 0 ||
    mention.related.length !== 1 || mention.related[0].idea.id !== 'mention') {
  throw new Error('text-only evidence was promoted into an analogue set');
}
''')

    def test_short_tokens_and_multiword_questions_use_exact_and_semantics(self):
        self.desk_runtime(r'''
const IDEAS = [
  observation({id:'fx', description:'FX carry was discussed.',
    _article:{title:'Currency carry note'}}),
  observation({id:'ai', description:'AI infrastructure was discussed.',
    _article:{title:'Technology infrastructure note'}}),
  observation({id:'vix', underlying:'VIX'}),
  observation({id:'jgb', underlying:'JGB'}),
  observation({id:'both', underlying:'VIX; JGB'}),
  observation({id:'noise', description:'Unrelated equity passage.'})
];

const fx = deskSets({structureFocus:'FX'});
if (fx.primary.length !== 0 || fx.related.map(function (row) {
  return row.idea.id;
}).join(',') !== 'fx') {
  throw new Error('FX was dropped or broadened to unrelated passages');
}
const ai = deskSets({structureFocus:'AI'});
if (ai.primary.length !== 0 || ai.related.map(function (row) {
  return row.idea.id;
}).join(',') !== 'ai') {
  throw new Error('AI was dropped or broadened to unrelated passages');
}

const both = deskSets({structureFocus:'VIX JGB'});
if (both.primary.length !== 1 || both.primary[0].idea.id !== 'both') {
  throw new Error('multiword focus used OR semantics: ' +
    both.primary.map(function (row) { return row.idea.id; }).join(','));
}
if (both.all.some(function (row) {
  return row.idea.id === 'vix' || row.idea.id === 'jgb';
})) {
  throw new Error('a partial token match entered an AND-constrained result');
}
''')

    def test_decision_question_is_required_for_handoff_but_never_changes_retrieval(self):
        self.desk_runtime(r'''
const IDEAS = [
  observation({id:'vix', underlying:'VIX'}),
  observation({id:'other', underlying:'JGB'})
];
const baseline = deskSets({structureFocus:'VIX'}).all.map(function (row) {
  return row.idea.id;
});
const firstQuestion = deskSets({structureFocus:'VIX',
  structureQuestion:'Should volatility exposure be reviewed?'}).all.map(function (row) {
  return row.idea.id;
});
const secondQuestion = deskSets({structureFocus:'VIX',
  structureQuestion:'Completely different analyst framing'}).all.map(function (row) {
  return row.idea.id;
});
if (baseline.join(',') !== 'vix' || firstQuestion.join(',') !== baseline.join(',') ||
    secondQuestion.join(',') !== baseline.join(',')) {
  throw new Error('analyst-authored decision wording changed deterministic retrieval');
}
if (!structureDecisionQuestion().includes('Completely different')) {
  throw new Error('the decision question was not retained separately for handoff');
}
''')

    def test_period_restricts_comparables_to_the_selected_record(self):
        self.desk_runtime(r'''
const IDEAS = [
  observation({id:'old', _article:{title:'',date:'2025-04-02',url:'https://example.test/a',source:'substack'}}),
  observation({id:'new', _article:{title:'',date:'2026-04-02',url:'https://example.test/b',source:'substack'}})
];
const periods = structurePeriodOptions().map(function (row) { return row[0]; });
if (periods.join(',') !== 'all,2026,2025') throw new Error('period options: ' + periods);
const only2025 = desk({structurePeriod:'2025'});
if (only2025.length !== 1 || only2025[0].idea.id !== 'old') {
  throw new Error('the period input did not restrict the comparison');
}
const spread = structurePattern(desk({structureInstrument:'equity'})).periods;
if (JSON.stringify(spread) !== '[["2026",1],["2025",1]]') {
  throw new Error('period distribution must run newest first: ' + JSON.stringify(spread));
}
''')

    def test_one_passage_naming_an_underlying_twice_counts_once(self):
        self.desk_runtime(r'''
const IDEAS = [
  observation({id:'repeat', underlying:'Crypto; crypto'}),
  observation({id:'single', underlying:'Crypto'})
];
const options = structureUnderlyingOptions();
const crypto = options.filter(function (row) { return normalize(row.label) === 'crypto'; })[0];
if (!crypto) throw new Error('the recurring underlying was not offered');
if (crypto.count !== 2) {
  throw new Error('a repeated mention inflated the precedent count to ' + crypto.count);
}
if (crypto.notes !== 2) {
  throw new Error('the recurring underlying lost its source-note breadth');
}
if (desk({structureFocus:'Crypto'}).length !== crypto.count) {
  throw new Error('the offered count disagrees with the comparables it returns');
}
''')

    def follow_up_runtime(self, threads, articles, assertions):
        run_node(
            javascript_between(
                'function normalize(value) {',
                'function articleBriefSearch',
            )
            + javascript_between(
                'function instrumentLabel(value) {',
                'function directionLabel(value) {',
            )
            + f'const THREADS = {threads};\n'
            + f'const ARTICLES = {articles};\n'
            + 'const ARTICLE_BY_ID = new Map(ARTICLES.map(function (a) '
              '{ return [a.id, a]; }));\n'
            + javascript_between(
                'const THREAD_ARTICLES = (function () {',
                '\nlet IDEAS = [];',
            )
            + javascript_between(
                'function underlyingParts(idea) {',
                'function structureChipRow(',
            )
            + assertions,
        )

    def test_follow_through_only_reports_later_notes_on_narrow_subjects(self):
        """Resolution must come from the record, not from loose association.

        A subject broad enough to span the archive would thread unrelated
        notes together and assert a continuity the record does not contain.
        """
        threads = """{
          article_count:100,
          defaults:{},
          topics:{
            narrow:{label:'Expected Shortfall',article_count:2,article_ids:['a0','a1','a3']},
            mid:{label:'S&P 500',article_count:5,article_ids:['a1','a2','a3']},
            wide:{label:'Options',article_count:40,article_ids:['a1','a4']}
          }
        }"""
        articles = """[
          {id:'a0',date:'2025-01-01',url:'https://example.test/0',title:'Earlier note'},
          {id:'a1',date:'2026-01-01',url:'https://example.test/1',title:'The observation'},
          {id:'a2',date:'2026-02-01',url:'https://example.test/2',title:'Mid follow-up'},
          {id:'a3',date:'2026-03-01',url:'https://example.test/3',title:'Narrow follow-up'},
          {id:'a4',date:'2026-04-01',url:'https://example.test/4',title:'Broad-subject note'}
        ]"""
        self.follow_up_runtime(threads, articles, r'''
if (FOLLOW_UP_MAX_TOPIC_ARTICLES !== 7) {
  throw new Error('subject breadth bound should scale with the archive: ' + FOLLOW_UP_MAX_TOPIC_ARTICLES);
}
const idea = {id:'i', _article:ARTICLE_BY_ID.get('a1')};
const followUps = observationFollowUps(idea);
const ids = followUps.map(function (row) { return row.article.id; });

if (ids.indexOf('a4') !== -1) {
  throw new Error('a subject spanning the archive must not imply continuity');
}
if (ids.indexOf('a0') !== -1) throw new Error('an earlier note is not a follow-up');
if (ids.indexOf('a1') !== -1) throw new Error('the observation followed itself');
if (ids.join(',') !== 'a3,a2') {
  throw new Error('the narrowest shared subject must rank first: ' + ids.join(','));
}
const narrow = followUps[0];
if (narrow.breadth !== 2) throw new Error('breadth should track the narrowest subject');
if (narrow.topics.indexOf('Expected Shortfall') === -1 ||
    narrow.topics.indexOf('S&P 500') === -1) {
  throw new Error('a note shared by two subjects must report both: ' + narrow.topics);
}
if (observationFollowUps(idea) !== followUps) throw new Error('resolution was not cached');
if (observationFollowUps({id:'x'}).length !== 0) {
  throw new Error('an observation with no article must resolve to nothing');
}
''')

    def test_rate_context_is_post_retrieval_provenance_only(self):
        """Treasury context can annotate, but never select or rank, evidence."""
        self.desk_runtime(r'''
const dated = function (id, date) {
  return {title:'', date:date, url:'https://example.test/' + id, source:'substack', id:id};
};
const IDEAS = [
  observation({id:'flat', _article:dated('a1','2026-01-05')}),
  observation({id:'steep', _article:dated('a2','2026-02-05')}),
  observation({id:'unpriced', _article:dated('a3','2030-01-01')})
];

const flat = rateReading(IDEAS[0]);
if (flat.y2 !== 4.10 || flat.y10 !== 4.40 || flat.slope !== 0.30) {
  throw new Error('curve reading was not read from the published series');
}
if (flat.asOf !== '2026-01-05') throw new Error('as-of date was lost');
const weekend = rateReading(IDEAS[1]);
if (weekend.asOf !== '2026-02-02') {
  throw new Error('a non-trading day must carry the close that produced it');
}
if (rateReading(IDEAS[2]) !== null) {
  throw new Error('a date the series does not cover must have no reading');
}

const baseline = desk({structureInstrument:'equity'}).map(function (row) {
  return row.idea.id;
});
if (baseline.length !== 3) throw new Error('the evidence setup lost a captured passage');
[
  {structureInstrument:'equity',structureMacro:true},
  {structureInstrument:'equity',structureMacro:true,structureSlope:'low'},
  {structureInstrument:'equity',structureMacro:true,structureSlope:'high'},
  {structureInstrument:'equity',structureMacro:true,structureLevel:'low'},
  {structureInstrument:'equity',structureMacro:true,structureLevel:'high'}
].forEach(function (patch) {
  const ids = desk(patch).map(function (row) { return row.idea.id; });
  if (ids.join(',') !== baseline.join(',')) {
    throw new Error('optional macro context changed evidence IDs or order: ' + ids);
  }
});
if (desk({structureInstrument:'equity',structureMacro:true}).some(function (row) {
  return row.reasons.join(' ').toLowerCase().includes('curve');
})) {
  throw new Error('post-retrieval macro context leaked into match reasons');
}

const pattern = structurePattern(desk({structureInstrument:'equity',structureMacro:true}));
const slopeTotal = pattern.slopeBands.reduce(function (sum, row) { return sum + row[1]; }, 0);
if (slopeTotal !== 2) {
  throw new Error('rate bands must tally only the passages the curve prices');
}
if (!rateSourceNote().includes('U.S. Treasury')) {
  throw new Error('the desk must cite the official series it conditions on');
}
if (!rateSourceNote().includes('not absolute regimes')) {
  throw new Error('relative bands must not read as regime claims');
}
''')

    def test_desk_note_clusters_sources_and_refuses_performance_claims(self):
        """The memo reports source-note coverage, not passage-level votes."""
        self.desk_runtime(r'''
// Owned URLs: the memo runs every citation through safeUrl, so an unowned
// host is rewritten to '#' rather than published as a link.
const dated = function (id, date) {
  return {title:'Note ' + id, date:date,
    url:'https://navnoorbawa.substack.com/p/note-' + id, source:'substack', id:id};
};
const IDEAS = [
  observation({id:'a1', instruments:['option','equity'], direction:'short',
    quant:'2x', _article:dated('a','2026-01-05')}),
  observation({id:'a2', instruments:['option'], direction:'short',
    _article:dated('a','2026-01-05')}),
  observation({id:'b', instruments:['option'], direction:'unspecified',
    _article:dated('b','2026-01-05')}),
  observation({id:'c', instruments:['option'], direction:'unspecified',
    _article:dated('c','2026-02-05')})
];
const ranked = desk({structureInstrument:'option'});
const pattern = structurePattern(ranked);
const lines = deskNoteLines(pattern);
const byLabel = new Map(lines.map(function (row) { return [row[0], row[1]]; }));

if (pattern.total !== 4 || pattern.noteTotal !== 3) {
  throw new Error('passages were mistaken for independent source notes');
}
if (!byLabel.get('Evidence set').includes('3 authored notes containing 4 matching extracted passages')) {
  throw new Error('the note did not state both source-note and passage breadth');
}
if (!byLabel.get('Evidence set').includes('Options')) {
  throw new Error('the note did not name its explicit retrieval constraint');
}
// The filtered instrument is in every row by construction; reporting it as
// 100% would say nothing, so the co-instrument is what gets reported.
const structured = byLabel.get('Parser candidates');
if (structured.includes('Options')) {
  throw new Error('the note restated the filter as a finding');
}
if (!structured.includes('Equity is mentioned in 1 of 3 authored notes') ||
    !structured.includes('not validated position legs')) {
  throw new Error('co-occurrence context was not bounded honestly: ' + structured);
}
if (!byLabel.get('Detected fields').includes(
    '1 of 3 authored notes contain a numeric phrase')) {
  throw new Error('note-clustered evidence coverage is wrong: ' +
    byLabel.get('Detected fields'));
}
const limits = byLabel.get('Primary diligence gap');
if (!limits.includes('No selected authored note contains a detected outcome / P&L phrase')) {
  throw new Error('the note must state the outcome gap');
}
if (!limits.includes('one-author, purposive archive') ||
    !limits.includes('performance base rate')) {
  throw new Error('passage frequency was allowed to read as performance evidence');
}

const markdown = deskNoteMarkdown(pattern, ranked);
if (!markdown.startsWith('# Research evidence memo')) throw new Error('memo has no title');
if (!markdown.includes('**Evidence set.**')) throw new Error('memo lost its sections');
if (!markdown.includes('## Primary retrieved authored notes')) throw new Error('memo cites nothing');
if (!markdown.includes('runtime-test-checksum')) throw new Error('memo lost snapshot provenance');
if (!markdown.includes('https://navnoorbawa.substack.com/p/note-a')) {
  throw new Error('memo lost its source links');
}
// An unowned citation must never reach the memo as a link.
const foreign = ranked.map(function (row) { return row; });
foreign[0].idea._article = {title:'Foreign', date:'2026-01-05',
  url:'https://evil.test/post', source:'substack', id:'x'};
if (deskNoteMarkdown(pattern, foreign).includes('evil.test')) {
  throw new Error('the memo published an unowned link');
}
foreign[0].idea._article = dated('a','2026-01-05');
if (markdown.includes(location.href)) {
  throw new Error('the memo leaked the local Structure URL');
}

// Raw counts remain raw even in a two-note set. Repeated passages from note a
// still count once, and the one-author archive boundary remains explicit.
const thinRows = ranked.filter(function (row) {
  return row.idea._article.id === 'a' || row.idea._article.id === 'b';
});
const thin = structurePattern(thinRows);
const thinText = deskNoteLines(thin).map(function (row) { return row[1]; }).join(' ');
if (thin.noteTotal !== 2 || thinText.includes('%') ||
    !thinText.includes('one-author, purposive archive')) {
  throw new Error('a thin source-note set was scored or lost its archive boundary');
}

// An empty class produces no note at all rather than a note about nothing.
const none = structurePattern(desk({structureFocus:'nothingmatches'}));
if (deskNoteLines(none).length !== 0) throw new Error('an empty class produced a note');
if (deskNoteMarkdown(none, []) !== '') throw new Error('an empty class produced a memo');
''')

    def test_structure_pattern_reports_what_the_record_actually_carries(self):
        self.desk_runtime(r'''
const IDEAS = [
  observation({id:'a1', instruments:['option','equity'], direction:'short',
    quant:'2x', outcome:'closed flat',_article:{id:'a'}}),
  observation({id:'a2', instruments:['option'], direction:'short',_article:{id:'a'}}),
  observation({id:'b', instruments:['option'], direction:'short', thesis:'carry'}),
  observation({id:'c', instruments:['equity'], direction:'unspecified'})
];
const pattern = structurePattern(desk({structurePeriod:'2026'}));
if (pattern.total !== 4 || pattern.noteTotal !== 3) {
  throw new Error('source-note clustering failed: ' + pattern.total + '/' + pattern.noteTotal);
}
if (pattern.withOutcome !== 1) throw new Error('outcome count must not be inflated');
if (pattern.withQuant !== 1 || pattern.withThesis !== 1) throw new Error('evidence counts are wrong');
const instruments = new Map(pattern.instruments);
if (instruments.get('option') !== 2 || instruments.get('equity') !== 2) {
  throw new Error('instrument tally is wrong: ' + JSON.stringify(pattern.instruments));
}
if ('combinations' in pattern) {
  throw new Error('lexical co-mentions were mislabeled as validated position legs');
}
const directions = new Map(pattern.directions);
if (directions.get('short') !== 2) throw new Error('stance tally is wrong');
if (directions.get('unspecified') !== 1) throw new Error('missing stance was hidden');
if (pattern.withCurrent !== 3 || pattern.withFull !== 3 || pattern.withReview !== 0) {
  throw new Error('source capture quality was not clustered by note');
}
''')


class DeskToDecisionRuntimeTests(unittest.TestCase):
    """An explicitly anchored source passage can seed only evidence fields.

    The bridge never chooses a passage for the analyst, never writes thesis or
    falsifier, and never leaves a half-written packet after persistence failure.
    """

    def bridge_runtime(self, assertions):
        run_node(
            javascript_between(
                'function normalize(value) {',
                'function articleBriefSearch',
            )
            + javascript_between(
                'function instrumentLabel(value) {',
                'function isArticleView()',
            )
            + javascript_between(
                'function publicationAccessLabel(article) {',
                'function sourceAccessLabel(article) {',
            )
            + javascript_between(
                'function reviewFlagged(idea) {',
                'function validDateInput',
            )
            + javascript_between(
                'function passageText(idea) {',
                'function ideaRow(idea) {',
            )
            + HARNESS_GLOBALS
            + javascript_between(
                'function underlyingParts(idea) {',
                'function structureChipRow(',
            )
            + javascript_between(
                'function structurePassageWarnings(idea) {',
                'function structurePassageMarkup(',
            )
            + javascript_between(
                'function countLabel(value,singular,plural) {',
                'function structureRelatedPanel(rows) {',
            )
            + javascript_between(
                'function deskShare(',
                'function renderStructureDesk(',
            )
            + javascript_between(
                'function clampWorkflowText(field,value) {',
                'function csvCell(value) {',
            )
            + HARNESS_QUEUE
            + assertions,
        )

    def test_the_selected_exact_passage_lands_in_an_evidence_only_packet(self):
        self.bridge_runtime(r"""
openDecisionPacketFromDesk();
const item = workflowItems.get('i1');
if (!item) throw new Error('the explicitly anchored passage opened no packet');
if (!item.note.includes('RESEARCH EVIDENCE SET') ||
    !item.note.includes('Selected exact passage: passage i1') ||
    !item.note.includes('EVIDENCE CHALLENGE')) {
  throw new Error('the packet did not retain its evidence boundary');
}
if (!item.note.includes('https://navnoorbawa.substack.com/p/note-i1')) {
  throw new Error('the packet lost its citations');
}
if (!item.note.includes('[ANCHOR] Note i1') ||
    !item.note.includes('runtime-test-checksum')) {
  throw new Error('the packet lost its selected source provenance');
}
if (item.thesis !== '' || item.falsifier !== '') {
  throw new Error('the desk wrote the analyst thesis or falsifier field');
}
if (!item.tags.includes('research evidence:')) throw new Error('packet was not tagged');
if (workflowItems.has('i2')) throw new Error('the desk silently chose another passage');
if (state.view !== 'queue' || state.selected !== 'i1') {
  throw new Error('the reader was not taken to the packet');
}
""")

    def test_a_long_packet_preserves_the_anchor_scope_and_final_challenge(self):
        """Bounded local storage must truncate optional detail, not safeguards."""
        self.bridge_runtime(r"""
IDEAS[0].description = 'anchor evidence '.repeat(55).slice(0,800);
IDEAS[0]._search = normalize([IDEAS[0]._article.title,IDEAS[0].description,
  IDEAS[0].direction,IDEAS[0].instruments.join(' '),IDEAS[0].underlying].join(' '));
for (let index = 3; index <= 7; index += 1) {
  const row = observation('i' + index);
  row._article.title = 'Long retrieved source note ' + index + ' ' + 'title '.repeat(32);
  row._article.url = 'https://navnoorbawa.substack.com/p/' +
    ('long-source-' + index + '-').repeat(8) + 'note';
  row._search = normalize([row._article.title,row.description,row.direction,
    row.instruments.join(' '),row.underlying].join(' '));
  IDEAS.push(row);
}
openDecisionPacketFromDesk();
const item = workflowItems.get('i1');
if (!item) throw new Error('long evidence set opened no packet');
if (item.note.length > WORKFLOW_TEXT_LIMITS.note) {
  throw new Error('bounded packet exceeded its storage contract');
}
if (!item.note.includes('Selected exact passage: ') ||
    !item.note.includes('PACKET EVIDENCE SCOPE') ||
    !item.note.includes('This packet cites 5 of 7 retrieved authored notes')) {
  throw new Error('long packet lost its anchor or explicit source-count scope');
}
if (!item.note.includes('What evidence would distinguish a real analogue from a narrative resemblance?')) {
  throw new Error('long packet truncation removed the final de-biasing question');
}
if (!item.note.includes('[Additional retrieved-source detail omitted')) {
  throw new Error('long packet silently truncated optional evidence detail');
}
""")

    def test_a_packet_requires_an_exact_passage_inside_the_selected_note(self):
        self.bridge_runtime(r"""
state.structureQuestion = '';
openDecisionPacketFromDesk();
if (workflowItems.size !== 0 ||
    !toast.includes('Enter a non-confidential decision question')) {
  throw new Error('a local task opened without an explicit analyst question');
}

toast = '';
state.structureQuestion = 'What published evidence should we verify?';
state.structurePassage = '';
openDecisionPacketFromDesk();
if (workflowItems.size !== 0 ||
    !toast.includes('Anchor the exact source passage')) {
  throw new Error('a note-level selection opened a packet without a passage');
}

toast = '';
state.structurePassage = 'i2';
openDecisionPacketFromDesk();
if (workflowItems.size !== 0 ||
    !toast.includes('Anchor the exact source passage')) {
  throw new Error('a passage from another source note was accepted');
}

state.structurePassage = 'i1';
openDecisionPacketFromDesk();
if (!workflowItems.has('i1')) {
  throw new Error('a valid explicit passage anchor was rejected');
}
""")

    def test_headline_only_context_passage_cannot_anchor_a_research_task(self):
        self.bridge_runtime(r"""
IDEAS[0]._article.title = 'HeadlineOnly market note';
IDEAS[0]._search = normalize([IDEAS[0]._article.title,IDEAS[0].description,
  IDEAS[0].direction,IDEAS[0].instruments.join(' '),IDEAS[0].underlying].join(' '));
state.structureFocus = 'HeadlineOnly';
state.structureAnchor = 'a_i1';
state.structurePassage = 'i1';
if (structureMatches().length !== 1 || structurePassageDirectMatch(IDEAS[0])) {
  throw new Error('the fixture did not isolate a headline-only contextual match');
}
openDecisionPacketFromDesk();
if (workflowItems.size !== 0 ||
    !toast.includes('Anchor the exact source passage')) {
  throw new Error('headline-only article context was allowed to seed a research task');
}
""")

    def test_the_desk_never_writes_the_analysts_own_view(self):
        self.bridge_runtime(r"""
openDecisionPacketFromDesk();
const item = workflowItems.get('i1');
// The desk supplies evidence and a prompt. The thesis is the analyst's.
if (item.thesis !== '' || item.falsifier !== '') {
  throw new Error('the desk wrote a thesis or falsifier view');
}

item.note = 'MY OWN NOTE';
item.falsifier = 'MY OWN FALSIFIER';
item.tags = 'my tag';
toast = '';
openDecisionPacketFromDesk();
if (item.note !== 'MY OWN NOTE' || item.falsifier !== 'MY OWN FALSIFIER' ||
    item.tags !== 'my tag') {
  throw new Error('re-opening overwrote entries the analyst had made');
}
if (!toast.includes('earlier Desk scope')) {
  throw new Error('the reader was not warned that the unchanged task has an earlier scope');
}
""")

    def test_a_failed_save_leaves_no_half_written_packet(self):
        self.bridge_runtime(r"""
openDecisionPacketFromDesk();
const item = workflowItems.get('i1');
item.note = 'MY OWN NOTE';

// Storage refuses: the packet must return to exactly its prior state.
persisted = false;
openDecisionPacketFromDesk();
if (workflowItems.get('i1').note !== 'MY OWN NOTE') {
  throw new Error('a refused save left the packet mutated');
}

// And a brand-new packet must not survive a refused save at all.
workflowItems.clear();
openDecisionPacketFromDesk();
if (workflowItems.size !== 0) {
  throw new Error('a refused save left a new packet behind');
}
""")

    def test_an_empty_analogue_set_opens_nothing(self):
        self.bridge_runtime(r"""
state.structureFocus = 'nothingmatchesthisword';
openDecisionPacketFromDesk();
if (workflowItems.size !== 0) {
  throw new Error('a packet was opened with no comparable to anchor it');
}
if (!toast.includes('Define a setup with primary retrieved evidence')) {
  throw new Error('the reader was not told why nothing opened');
}
""")


if __name__ == '__main__':
    unittest.main()
