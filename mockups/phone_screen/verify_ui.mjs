// Logic checks in Node. These complement, not replace, the in-app browser run.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import vm from 'node:vm';
const html = await fs.readFile(new URL('./index.html', import.meta.url), 'utf8');
const state = JSON.parse(await fs.readFile(new URL('./phone_state.json', import.meta.url), 'utf8'));
const elements = new Map();
const element = id => {
  if (!elements.has(id)) elements.set(id, {id, innerHTML:'', textContent:'', hidden:id==='settings', value:'', tagName:'DIV',
    classList:{toggle(){}}, style:{setProperty(){}}, focus(){}, isConnected:true, querySelectorAll(){return [];}});
  return elements.get(id);
};
const emitted=[];
const context=vm.createContext({console, Date, Math, Number, JSON, Object, Array, String, Error,
  document:{getElementById:element,querySelectorAll(){return [];},activeElement:element('main')},
  window:{addEventListener(){},dispatchEvent(e){emitted.push(e);},isSecureContext:true},
  navigator:{clipboard:{async writeText(){}}},
  CustomEvent:class {constructor(type, options){this.type=type;this.detail=options.detail;}},
  requestAnimationFrame(fn){fn();},setInterval(){},
  fetch:async()=>({ok:true,json:async()=>state})});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
await new Promise(resolve=>setImmediate(resolve));
const run=code=>vm.runInContext(code,context);
let passed=0;
function check(name,fn){fn();passed++;console.log('PASS '+name);}
check('Angle wrap at north',()=>{assert.equal(run('wrap(-2)'),358);assert.equal(run('delta(1,359)'),2);});
check('GPS gate starts closed',()=>assert.match(element('main').innerHTML,/id="start"[^>]*disabled/));
check('GPS loss enables terrain UI',()=>{run('toggleGPS()');assert.match(element('main').innerHTML,/Terrain mode active/);assert.doesNotMatch(element('main').innerHTML,/id="start"[^>]*disabled/);});
check('Misaligned peak cannot be locked',()=>{run("navigate('aim');heading=0;lock()");assert.equal(run('locks.length'),0);});
check('Skip cycles without recording',()=>{const first=run('targetId()');run('skipTarget()');assert.notEqual(run('targetId()'),first);run('skipTarget();skipTarget()');assert.equal(run('targetId()'),first);assert.equal(run('locks.length'),0);});
check('First lock switches away from reference panorama',()=>{run('heading=target().bearing_deg;elevation=target().elevation_deg;lock()');assert.equal(run('locks.length'),1);assert.equal(run('panoramaStage()===currentStage()'),true);});
check('Three distinct locks give guide and observation contract',()=>{run('heading=target().bearing_deg;elevation=target().elevation_deg;lock();heading=target().bearing_deg;elevation=target().elevation_deg;lock()');assert.equal(run('screen'),'guide');assert.equal(run('new Set(locks.map(x=>x.peak_id)).size'),3);assert.equal(emitted.length,3);assert.deepEqual(Object.keys(emitted[0].detail).sort(),['bearing_true_deg','elevation_deg','peak_id','peak_name','timestamp']);});
check('18 degree mode removes movement arrow',()=>{run('setInjury(true)');assert.equal(run('screen'),'stay');assert.match(element('main').innerHTML,/Stay\./);assert.doesNotMatch(element('main').innerHTML,/arrow-dial/);});
check('Radio report retains injury and has no send action',()=>{run('restoreRadio()');const report=run('report()');assert.equal(report.transmitted,false);assert.equal(report.movement,'stay_signal_wait');assert.ok(Date.parse(report.fix_timestamp));assert.equal(report.confidence_radius_m,state.stages.at(-1).confidence_radius_m);});
check('Restoring healthy mode restores arrow',()=>{run('setInjury(false)');assert.equal(run('screen'),'guide');assert.match(element('main').innerHTML,/arrow-dial/);});
check('Source adapter rejects unknown source',()=>assert.throws(()=>run("setSource('gps')"),/Unknown input/));
check('Box accepts current true-north sample',()=>{run("setSource('box');ingest({heading_deg:359,elevation_deg:12,north_reference:'true',timestamp:new Date().toISOString()},'box')");assert.equal(run('heading'),359);assert.equal(run('usable()'),true);});
check('Magnetic conversion wraps across north',()=>{run("declination=10;ingest({heading_deg:355,elevation_deg:5,north_reference:'magnetic',timestamp:new Date().toISOString()},'box')");assert.equal(run('heading'),5);});
check('Wrong source cannot replace current sample',()=>{assert.equal(run("ingest({},'device')"),false);assert.equal(run('heading'),5);});
check('Non-finite or out-of-range samples rejected',()=>{for(const h of ['NaN','Infinity','-1','360','null'])assert.throws(()=>run(`ingest({heading_deg:${h},elevation_deg:0,north_reference:'true',timestamp:new Date().toISOString()},'box')`));assert.throws(()=>run("ingest({heading_deg:0,elevation_deg:91,north_reference:'true',timestamp:new Date().toISOString()},'box')"));});
check('Stale and future samples rejected',()=>{for(const n of [-5000,2000])assert.throws(()=>run(`ingest({heading_deg:0,elevation_deg:0,north_reference:'true',timestamp:new Date(Date.now()+${n}).toISOString()},'box')`),/stale/);});
check('Relative input is unusable before calibration',()=>{run("setSource('device');ingest({heading_deg:20,elevation_deg:15,north_reference:'relative',timestamp:new Date().toISOString()},'device')");assert.equal(run('usable()'),false);});
check('Lost stream suppresses movement guidance',()=>{run('calibrated=true;lastSensor=Date.now()-5000;drawOrientation()');assert.equal(run('usable()'),false);assert.equal(element('arrow-dial').hidden,true);});
check('Upright north-facing phone vector',()=>{run("setSource('device');deviceSample({alpha:0,beta:90,gamma:0,absolute:true})");assert.ok(Math.abs(run('heading'))<1e-9);assert.ok(Math.abs(run('elevation'))<1e-9);});
check('Upright west-facing phone vector',()=>{run('deviceSample({alpha:90,beta:90,gamma:0,absolute:true})');assert.equal(run('heading'),270);});
check('Fresh fix resets observations when restarted',()=>{run("navigate('sensors');document.getElementById('start').onclick()");assert.equal(run('locks.length'),0);assert.equal(run('fixTime'),null);});
check('No runtime network dependencies or location requests',()=>{assert.doesNotMatch(html,/<script[^>]+src=|https?:\/\//);assert.doesNotMatch(html,/navigator\.geolocation|sendBeacon|new WebSocket/);});
console.log(`${passed} logic checks passed. Browser screenshots and responsive checks are separate evidence.`);
