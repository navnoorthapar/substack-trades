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
const PAGE_SIZE = {briefing:24,ideas:50,research:80,queue:100};
const THREAD_ARTICLES = {
  a_current:{topics:['vix','options'],default_topic:'vix'}
};
const MANAGERS = [];
const VALID_SOURCES = new Set();
const VALID_BODY_REVISIONS = new Set(['current','prior','unverified']);
const VALID_DIRECTIONS = new Set();
const VALID_INSTRUMENTS = new Set();
const VALID_QUALITY = new Set();
const VALID_PUBLICATION_ACCESS = new Set(['public','member','unknown']);
const VALID_CONTENT = new Set();
const VALID_QUEUE_STATUSES = new Set();
const VALID_DOCUMENTATION = new Set(['all']);
const VALID_BRIEF_LENSES = new Set(['all']);
const storedDensity = 'compact';
const setFromParam = () => new Set();
const state = {
  view:'briefing',query:'',sources:new Set(),revisions:new Set(),directions:new Set(),
  instruments:new Set(),managers:new Set(),quality:new Set(),publicationAccess:new Set(),content:new Set(),
  queueStatuses:new Set(),documentation:'all',newOnly:false,range:'all',
  coverage:'all',briefLens:'all',threadTopic:'',sort:'newest',
  density:'compact',selected:'',limit:24
};
const historyCalls = [];
globalThis.history = {
  pushState(_state,_title,target) { historyCalls.push(['push',target]); },
  replaceState(_state,_title,target) { historyCalls.push(['replace',target]); }
};
globalThis.location = {
  hash:'#selected=a_current&topic=vix',pathname:'/terminal/',search:''
};
'''
            + functions
            + r'''
hydrateFromHash();
if (state.threadTopic !== 'vix') throw new Error('owned thread topic was rejected');

location.hash = '#selected=a_current&topic=unowned';
hydrateFromHash();
if (state.threadTopic !== '') throw new Error('unowned thread topic was accepted');
updateHash();
if (historyCalls.length !== 1 || historyCalls[0][0] !== 'replace' ||
    historyCalls[0][1] !== '#selected=a_current' ||
    historyCalls[0][1].includes('topic=')) {
  throw new Error('invalid topic hash was not canonicalized safely');
}

location.hash = '#view=ideas&selected=a_current&topic=vix';
hydrateFromHash();
if (state.threadTopic !== '') throw new Error('topic escaped the briefing view');
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
    """The structure desk ranks comparable observations for a trading decision.

    Its ranking has to stay honest under thin data: a hard input must exclude
    rather than pad the comparison, and a passage that names an underlying
    twice must not look like two independent precedents.
    """

    def desk_runtime(self, assertions):
        run_node(
            javascript_between(
                'function normalize(value) {',
                'function articleBriefSearch',
            )
            + javascript_between(
                'function instrumentLabel(value) {',
                'function directionLabel(value) {',
            )
            + javascript_between(
                'function escapeHtml(value) {',
                'function safeUrl(value) {',
            )
            + r'''
const THREADS = {article_count:0,defaults:{},topics:{}};
const THREAD_ARTICLES = Object.create(null);
const ARTICLE_BY_ID = new Map();
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
            + r'''
function observation(overrides) {
  const idea = Object.assign({
    id:'i_0', description:'', direction:'unspecified', instruments:['equity'],
    underlying:'', thesis:'', quant:'', outcome:'', manager:'',
    _article:{title:'',date:'2026-01-01',url:'https://example.test/a',source:'substack'}
  }, overrides);
  idea._search = normalize([
    idea._article.title, idea.description, idea.direction,
    idea.instruments.join(' '), idea.underlying, idea.thesis, idea.quant,
    idea.outcome, idea.manager
  ].join(' '));
  return idea;
}
function desk(patch) {
  Object.assign(state, {
    structureFocus:'', structureInstrument:'', structureDirection:'any',
    structurePeriod:'all', structureSlope:'any', structureLevel:'any'
  }, patch || {});
  return structureMatches();
}
const state = {};
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

    def test_named_underlying_outranks_a_passing_text_mention(self):
        self.desk_runtime(r'''
const IDEAS = [
  observation({id:'named', underlying:'VIX', quant:'19 handle'}),
  observation({id:'mention', description:'A note that mentions VIX only in passing.'})
];
const ranked = desk({structureFocus:'VIX'});
if (ranked.length !== 2) throw new Error('both passages should be comparable');
if (ranked[0].idea.id !== 'named') {
  throw new Error('a named underlying must outrank an incidental mention');
}
if (ranked[0].score <= ranked[1].score) throw new Error('ranking scores did not separate');
if (!ranked[0].reasons.join(' ').includes('Same underlying')) {
  throw new Error('the named-underlying match must say so');
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
const spread = structurePattern(desk({})).periods;
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

    def test_rate_conditions_come_from_the_published_curve(self):
        """Comparables can be conditioned on the curve at publication.

        The reading must be a published close: a date that is not a trading
        day carries the as-of day that produced it, never an interpolation.
        """
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

const flattest = desk({structureSlope:'low'}).map(function (r) { return r.idea.id; });
if (flattest.join(',') !== 'flat') {
  throw new Error('curve-shape input did not select its band: ' + flattest);
}
const steepest = desk({structureSlope:'high'});
if (steepest.length !== 1 || steepest[0].idea.id !== 'steep') {
  throw new Error('curve-shape input did not select the steeper band');
}
if (!steepest[0].reasons.join(' ').toLowerCase().includes('curve shape')) {
  throw new Error('a rate-conditioned match must say the curve was matched');
}
const level = desk({structureLevel:'high'}).map(function (r) { return r.idea.id; });
if (level.join(',') !== 'steep') throw new Error('10Y level input did not select: ' + level);

// An observation the curve cannot price is excluded rather than guessed at.
const all = desk({});
if (all.length !== 3) throw new Error('an unconditioned desk should keep every passage');
const conditioned = desk({structureSlope:'low'}).concat(desk({structureSlope:'mid'}))
  .concat(desk({structureSlope:'high'})).map(function (r) { return r.idea.id; });
if (conditioned.indexOf('unpriced') !== -1) {
  throw new Error('an observation with no curve reading was given a band');
}

const pattern = structurePattern(desk({}));
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

    def test_structure_pattern_reports_what_the_record_actually_carries(self):
        self.desk_runtime(r'''
const IDEAS = [
  observation({id:'a', instruments:['option','equity'], direction:'short', quant:'2x', outcome:'closed flat'}),
  observation({id:'b', instruments:['option'], direction:'short', thesis:'carry'}),
  observation({id:'c', instruments:['equity'], direction:'unspecified'})
];
const pattern = structurePattern(desk({}));
if (pattern.total !== 3) throw new Error('pattern total: ' + pattern.total);
if (pattern.withOutcome !== 1) throw new Error('outcome count must not be inflated');
if (pattern.withQuant !== 1 || pattern.withThesis !== 1) throw new Error('evidence counts are wrong');
const instruments = new Map(pattern.instruments);
if (instruments.get('option') !== 2 || instruments.get('equity') !== 2) {
  throw new Error('instrument tally is wrong: ' + JSON.stringify(pattern.instruments));
}
if (pattern.combinations[0][0] !== 'Equity + Options' || pattern.combinations[0][1] !== 1) {
  throw new Error('combination tally is wrong: ' + JSON.stringify(pattern.combinations));
}
const directions = new Map(pattern.directions);
if (directions.get('short') !== 2) throw new Error('stance tally is wrong');
''')


if __name__ == '__main__':
    unittest.main()
