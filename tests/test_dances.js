// Run with: node annotation_tool/tests/test_dances.js
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(`${__dirname}/../static/app.js`, 'utf8');
const elements = new Map();
vm.runInNewContext(source.replace('safely(init);', '') + `
function check(value, message) { if (!value) throw new Error(message); }
function same(actual, expected, message) { check(JSON.stringify(actual) === JSON.stringify(expected), message); }
const dances = [
  {id:'a', name:'First', run_ids:['r1','r2'], notes:'First note', uncertain:false},
  {id:'b', name:'Keep me', run_ids:['r3'], notes:'Second note', uncertain:false},
  {id:'c', name:'Third', run_ids:['r4'], notes:'First note', uncertain:true},
  {id:'d', name:'Untouched', run_ids:['r5'], notes:'Other note', uncertain:false},
];
const original = clone(dances);
const result = mergedDances(dances, ['a','b','c'], 'b');
same(dances, original, 'Merge must not mutate the input before a successful save');
same(result.map(d=>d.id), ['b','d'], 'Only merged source dances are removed');
check(result[0].name === 'Keep me' && result[0].uncertain, 'Keep target name and propagate uncertainty');
same(result[0].run_ids, ['r1','r2','r3','r4'], 'Every member is retained once');
same(result[0].notes, 'Second note\\n\\nFirst note', 'Keep target notes first and preserve other distinct notes');
same(result[1], dances[3], 'Unselected dance stays unchanged');
for (const [ids, target] of [[['a'],'a'], [['a','missing'],'a'], [['a','b'],'c']]) {
  let failed = false;
  try { mergedDances(dances, ids, target); } catch (_) { failed = true; }
  check(failed, 'Reject incomplete or stale merge selections');
}
const clips = ['r1','r2','r3','r4','r5','ungrouped'].map((id,i)=>({id,
  annotation:{status:'accepted'}, frames:[{source_time_s:i}]}));
state.project = {dances, clips};
$('comb-time-start').value = '';$('comb-time-end').value = '';
state.danceFilters = new Set(['a','c']);
same(visibleRuns().map(c=>c.id), ['r1','r2','r4'], 'Display the union of selected dances');
$('comb-time-start').value = '1';$('comb-time-end').value = '2';
same(visibleRuns().map(c=>c.id), ['r2'], 'Time filters still apply to multiple dances');
$('comb-time-start').value = '';$('comb-time-end').value = '';
state.danceFilters.clear();
check(visibleRuns().length === 6, 'No selected dances shows all runs including ungrouped');
state.danceFilters.add('deleted');state.mergeSelection.add('deleted');
visibleRuns();
check(!state.danceFilters.size && !state.mergeSelection.size, 'Discard removed dance IDs');
state.danceSource = 'pipeline';state.pipeline = {dances:[dances[1]],clips};
state.danceFilters.add('b');
same(visibleRuns().map(c=>c.id), ['r3'], 'Filtering also supports pipeline dances');
`, {
  window: {ANNOTATION_CONFIG: {}},
  document: {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {value:'', checked:false});
    return elements.get(id);
  }},
});
console.log('Dance merge and multiple-display checks passed');
