/* Read-only mobile contract check. All patching happens in memory. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

function correctedSource(source) {
  const anchor = 'function checkType(';
  const helper = `function unionOptions(type: string): string[] {
  let depth = 0;
  let start = 0;
  const options: string[] = [];
  for (let index = 0; index < type.length; index += 1) {
    if (type[index] === '<') depth += 1;
    else if (type[index] === '>') depth -= 1;
    else if (depth === 0 && type.slice(index, index + 4) === ' or ') {
      options.push(type.slice(start, index));
      start = index + 4;
      index += 3;
    }
  }
  options.push(type.slice(start));
  return options;
}
`;
  if (!source.includes('function unionOptions(')) {
    assert(source.includes(anchor), 'Mobile checkType anchor changed; review the patch.');
    source = source.replace(anchor, helper + anchor);
    assert(source.includes("if (type.includes(' or ')) {"));
    source = source.replace("if (type.includes(' or ')) {", "const options = unionOptions(type);\n  if (options.length > 1) {");
    source = source.replace("for (const option of type.split(' or ')) {", 'for (const option of options) {');
  }
  if (!source.includes("type === 'DraftRegistryConfig'")) {
    const marker = "  if (type === 'InstitutionDiscovery') {";
    assert(source.includes(marker), 'Mobile registry anchor changed; review the patch.');
    source = source.replace(marker, `  if (type === 'DraftRegistryConfig' || type === 'RegistryConfig') {
    if (!isRecord(value)) throw new Error(\`Invalid registry config \${path}\`);
    return;
  }
` + marker);
  }
  return source;
}

function correctedCatalog(catalog) {
  const definitions = JSON.parse(fs.readFileSync(path.join(__dirname, '../models/institution/definitions.json'), 'utf8'));
  const result = JSON.parse(JSON.stringify(catalog));
  const pending = Object.keys(result);
  const seen = new Set();
  while (pending.length) {
    const name = pending.pop();
    if (seen.has(name)) continue;
    seen.add(name);
    const spec = result[name];
    for (const type of [...spec.fields.map(field => field.type), spec.extends || '']) {
      for (const reference of type.match(/\b[A-Z][A-Za-z0-9]+\b/g) || []) {
        if (!result[reference] && definitions[reference]) {
          const definition = definitions[reference];
          result[reference] = {
            extends: definition.extends || null,
            fields: Object.entries(definition.fields).map(([name, field]) => ({ name, type: field.type, presence: field.presence, description: field.rule })),
          };
          pending.push(reference);
        }
      }
    }
  }
  return result;
}

function correctedCatalogText(original) {
  const catalog = JSON.parse(original);
  const corrected = correctedCatalog(catalog);
  const additions = Object.fromEntries(Object.entries(corrected).filter(([name]) => !catalog[name]));
  if (!Object.keys(additions).length) return original;
  const newline = original.includes('\r\n') ? '\r\n' : '\n';
  const tail = JSON.stringify(additions, null, 2).slice(2, -2).replace(/\n/g, newline);
  return original.trimEnd().slice(0, -1).trimEnd() + ',' + newline + tail + newline + '}' + newline;
}

function load(frontend, source, includeCatalogFix = false) {
  const typescript = require(path.join(frontend, 'node_modules/typescript'));
  const directory = path.join(frontend, 'src/features/institution/contracts');
  const code = typescript.transpileModule(source, {
    compilerOptions: { module: typescript.ModuleKind.CommonJS, target: typescript.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const exports = {};
  vm.runInNewContext(code, { exports, require(name) {
    if (name === './wireCatalog.json' || name === './enumCatalog.json') {
      const value = JSON.parse(fs.readFileSync(path.join(directory, name), 'utf8'));
      return name === './wireCatalog.json' && includeCatalogFix ? correctedCatalog(value) : value;
    }
    if (name.includes('geoJson')) return { isUsableBoundary: geometry => geometry.type === 'Polygon' && Array.isArray(geometry.coordinates) && geometry.coordinates[0]?.length >= 4 };
    throw new Error(`Unexpected dependency: ${name}`);
  } });
  return exports;
}

if (require.main === module) {
  const frontend = path.resolve(process.argv[2] || path.join(__dirname, '../../TerraTrustAR'));
  const file = path.join(frontend, 'src/features/institution/contracts/schemas.ts');
  const original = fs.readFileSync(file, 'utf8');
  const fixtures = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const before = load(frontend, original);
  const after = load(frontend, correctedSource(original), true);
  const failures = [];
  for (const fixture of fixtures) {
    try { before.validateModel(fixture.model, fixture.value); }
    catch (error) { failures.push(`${fixture.model}: ${error.message}`); }
    after.validateModel(fixture.model, fixture.value);
  }
  // Reject bad input after the fix: the patch must not disable validation.
  assert.throws(() => after.validateModel('AnalyticsPeriod', { ...fixtures.find(f => f.model === 'AnalyticsPeriod').value, by_scope: { SCOPE_1: 12 } }));
  assert.throws(() => after.validateModel('RegistryResource', { ...fixtures.find(f => f.model === 'RegistryResource').value, config: [] }));
  assert.throws(() => after.validateModel('Denominators', {}));
  console.log(JSON.stringify({ checked: fixtures.length, original_failures: [...new Set(failures)], patched_validator: 'PASS', frontend_files_modified: false }));
}

module.exports = { correctedSource, correctedCatalogText };
