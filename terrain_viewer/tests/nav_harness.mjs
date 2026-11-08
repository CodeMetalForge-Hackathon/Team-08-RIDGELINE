// Runs terrain_viewer/team8_nav.js in Node with no browser and no packages: a small PNG decoder (zlib is built in)
// stands in for <img> + <canvas>, so the tests use the exact planner the viewer and the RIDGELINE phone load.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import zlib from 'node:zlib';
import {fileURLToPath} from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const TV = path.resolve(HERE, '..');

export function decodePNG(buf) {
  if (buf.readUInt32BE(0) !== 0x89504e47) throw new Error('not a PNG');
  let p = 8, w = 0, h = 0, depth = 0, type = 0, inter = 0, pal = null, trns = null;
  const idat = [];
  while (p < buf.length) {
    const len = buf.readUInt32BE(p), kind = buf.toString('ascii', p + 4, p + 8), d = buf.subarray(p + 8, p + 8 + len);
    if (kind === 'IHDR') { w = d.readUInt32BE(0); h = d.readUInt32BE(4); depth = d[8]; type = d[9]; inter = d[12]; }
    else if (kind === 'PLTE') pal = d;
    else if (kind === 'tRNS') trns = d;
    else if (kind === 'IDAT') idat.push(d);
    else if (kind === 'IEND') break;
    p += 12 + len;
  }
  if (depth !== 8 || inter) throw new Error(`PNG depth ${depth} interlace ${inter} not supported`);
  const ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[type], stride = w * ch, raw = zlib.inflateSync(Buffer.concat(idat));
  const px = new Uint8Array(h * stride);
  for (let y = 0; y < h; y++) {
    const f = raw[y * (stride + 1)], src = y * (stride + 1) + 1, row = y * stride;
    for (let x = 0; x < stride; x++) {
      const a = x >= ch ? px[row + x - ch] : 0, b = y ? px[row - stride + x] : 0, c = x >= ch && y ? px[row - stride + x - ch] : 0;
      let v = raw[src + x];
      if (f === 1) v += a; else if (f === 2) v += b; else if (f === 3) v += (a + b) >> 1;
      else if (f === 4) { const q = a + b - c, pa = Math.abs(q - a), pb = Math.abs(q - b), pc = Math.abs(q - c); v += pa <= pb && pa <= pc ? a : pb <= pc ? b : c; }
      px[row + x] = v & 255;
    }
  }
  const rgba = new Uint8ClampedArray(w * h * 4);
  for (let i = 0; i < w * h; i++) {
    let r, g, b, al = 255;
    if (type === 0) r = g = b = px[i];
    else if (type === 4) { r = g = b = px[2 * i]; al = px[2 * i + 1]; }
    else if (type === 2) { r = px[3 * i]; g = px[3 * i + 1]; b = px[3 * i + 2]; }
    else if (type === 6) { r = px[4 * i]; g = px[4 * i + 1]; b = px[4 * i + 2]; al = px[4 * i + 3]; }
    else { const k = px[i]; r = pal[3 * k]; g = pal[3 * k + 1]; b = pal[3 * k + 2]; if (trns && k < trns.length) al = trns[k]; }
    rgba[4 * i] = r; rgba[4 * i + 1] = g; rgba[4 * i + 2] = b; rgba[4 * i + 3] = al;
  }
  return {width: w, height: h, data: rgba};
}

let installed = false;
function install() {
  if (installed) return;
  installed = true;
  globalThis.window = globalThis;
  globalThis.Image = class {
    set src(u) {
      this._src = u;
      setImmediate(() => {
        try {
          const m = /^data:image\/png;base64,(.*)$/.exec(u);
          const im = decodePNG(m ? Buffer.from(m[1], 'base64') : fs.readFileSync(u));
          Object.assign(this, {width: im.width, height: im.height, naturalWidth: im.width, naturalHeight: im.height, _px: im.data});
          this.onload && this.onload();
        } catch (e) { this.onerror ? this.onerror(e) : (() => { throw e; })(); }
      });
    }
    get src() { return this._src; }
  };
  globalThis.document = {
    createElement(tag) {
      if (tag !== 'canvas') throw new Error('only canvas');
      const cv = {width: 0, height: 0, _px: null};
      cv.getContext = () => ({
        drawImage(im) { cv._px = im._px; },
        getImageData() { return {width: cv.width, height: cv.height, data: cv._px}; },
      });
      return cv;
    },
  };
}

const cache = new Map();
// a fresh planner for one area, the same object index.html and the phone get from TEAM8_NAV.init(TEAM8)
export async function loadNav(id = 'moran') {
  if (cache.has(id)) return cache.get(id);
  install();
  vm.runInThisContext(fs.readFileSync(path.join(TV, 'team8_nav.js'), 'utf8'), {filename: 'team8_nav.js'});
  vm.runInThisContext(fs.readFileSync(path.join(TV, 'data', id, 'bundle.js'), 'utf8'), {filename: `${id}/bundle.js`});
  const D = globalThis.TEAM8, nav = await globalThis.TEAM8_NAV.init(D);
  const out = {nav, D};
  cache.set(id, out);
  return out;
}
