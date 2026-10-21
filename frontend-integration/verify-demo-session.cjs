/* Offline checks of the actual TypeScript handoff with mocked app boundaries. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const frontend = path.resolve(process.argv[2] || path.join(__dirname, '../../TerraTrustAR'));
const sourcePath = path.resolve(process.argv[3] || path.join(frontend, 'src/features/institution/services/demoSession.ts'));
const ts = require(path.join(frontend, 'node_modules/typescript'));
const compiled = ts.transpileModule(fs.readFileSync(sourcePath, 'utf8'), {
  compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS},
});

async function journey(account, changes = {}, purpose = 'SIGN_IN', flags = {ENABLE_INSTITUTION_DEMO_ACCOUNTS: 'true', ENABLE_FARMER_DEMO_ACCOUNTS: 'false'}) {
  const intent = {purpose, flow: 'INSTITUTION', attemptId: '00000000-0000-4000-8000-000000000001'};
  const user = {uid: 'offline-demo', phoneNumber: `+91900000000${account}`};
  const receipt = {
    session_id: '00000000-0000-4000-8000-000000000002', status: 'READY',
    account_number: account, account_domain: 'INSTITUTION', checkpoint_key: 'source',
    reset_performed: account !== 9, fixture_generation: 1,
    reset_policy: account === 9 ? 'PERSISTENT' : 'NEXT_LOGIN',
    audit_demo_mode: true, poll_after_seconds: null, safe_error: null, ...changes,
  };
  let requests = 0, purges = 0;
  const api = {post: async () => {requests++; return {data: {data: receipt}};}};
  const mocks = {
    '../../../services/api': {default: api},
    '../../../services/loginCoordinator': {getLoginIntent: () => intent},
    '../../../services/firebase': {getCurrentFirebaseUser: () => user},
    'react-native-config': {default: flags},
    '../../ar-audit/utils/demoMode': {parseBooleanEnv: value => ['true', '1', 'yes', 'on'].includes((value || '').trim().toLowerCase())},
    '../contracts/schemas': {decodeSuccess: value => value, uuid: value => assert.match(value, /^[0-9a-f-]{36}$/)},
    '../storage/vault': {purgeInstitutionAccount: async () => {purges++;}},
  };
  const context = {exports: {}, require: name => {
    if (!(name in mocks)) throw new Error(`Unexpected boundary: ${name}`);
    return mocks[name];
  }, setTimeout};
  vm.runInNewContext(compiled.outputText, context, {filename: sourcePath});
  const result = await context.exports.institutionDemoHandoff(intent);
  return {result, requests, purges};
}

(async () => {
  let checks = 0;
  for (const account of [5, 6, 7, 8]) {
    const outcome = await journey(account);
    assert.equal(outcome.requests, 1);
    assert.equal(outcome.purges, 1);
    checks++;
  }
  const persistent = await journey(9);
  assert.equal(persistent.requests, 1);
  assert.equal(persistent.purges, 0);
  assert.equal(persistent.result.reset_policy, 'PERSISTENT'); checks++;
  assert.equal((await journey(9, {}, 'RESUME')).requests, 0); checks++;
  assert.equal((await journey(5, {}, 'SIGN_IN', {ENABLE_FARMER_DEMO_ACCOUNTS: 'true', ENABLE_INSTITUTION_DEMO_ACCOUNTS: 'false'})).requests, 0); checks++;
  assert.equal((await journey(5, {}, 'SIGN_IN', {ENABLE_FARMER_DEMO_ACCOUNTS: 'false', ENABLE_INSTITUTION_DEMO_ACCOUNTS: 'true'})).requests, 1); checks++;
  for (const [account, change] of [[9, {reset_policy: 'NEXT_LOGIN'}], [9, {reset_performed: true}], [8, {reset_performed: false}], [9, {fixture_generation: 0}], [9, {account_number: 8}]]) {
    await assert.rejects(journey(account, change)); checks++;
  }
  const farmerPath = path.resolve(process.argv[4] || path.join(frontend, 'src/features/ar-audit/utils/demoMode.ts'));
  const farmerCode = ts.transpileModule(fs.readFileSync(farmerPath, 'utf8'), {
    compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS},
  }).outputText;
  for (const [flags, expected] of [
    [{ENABLE_FARMER_DEMO_ACCOUNTS: 'false', ENABLE_INSTITUTION_DEMO_ACCOUNTS: 'true', AUDIT_DEMO_MODE: 'true'}, false],
    [{ENABLE_FARMER_DEMO_ACCOUNTS: 'true', ENABLE_INSTITUTION_DEMO_ACCOUNTS: 'false'}, true],
    [{AUDIT_DEMO_MODE: 'true', ENABLE_INSTITUTION_DEMO_ACCOUNTS: 'false'}, true],
    [{ENABLE_INSTITUTION_DEMO_ACCOUNTS: 'true'}, false],
  ]) {
    const context = {exports: {}, require: name => {
      assert.equal(name, 'react-native-config'); return {default: flags};
    }};
    vm.runInNewContext(farmerCode, context, {filename: farmerPath});
    assert.equal(context.exports.IS_AUDIT_DEMO_MODE, expected); checks++;
  }
  process.stdout.write(`${checks} frontend demo handoff checks passed; no device/provider/database access.\n`);
})().catch(error => {process.stderr.write(`${error.stack}\n`); process.exitCode = 1;});
