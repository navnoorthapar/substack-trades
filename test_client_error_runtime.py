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
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)


class ClientErrorRuntimeTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
