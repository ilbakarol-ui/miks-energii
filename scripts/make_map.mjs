// Generuje mixtracker/europe_map.json: granice państw (Natural Earth 1:50m z pakietu world-atlas)
// rzutowane na płaszczyznę (rzut stożkowy wiernokątny) jako ścieżki SVG.
// Uruchomienie: npm install world-atlas@2.0.2 topojson-client@3.1.0 d3-geo@3.1.1 && node scripts/make_map.mjs
import fs from "fs";
import { feature } from "topojson-client";
import { geoConicConformal, geoPath } from "d3-geo";
const topo = JSON.parse(fs.readFileSync("node_modules/world-atlas/countries-50m.json"));
const fc = feature(topo, topo.objects.countries);
const ISO = {"040":"AT","056":"BE","100":"BG","191":"HR","196":"CY","203":"CZ","208":"DK","233":"EE","246":"FI","250":"FR","276":"DE","300":"GR","348":"HU","372":"IE","380":"IT","428":"LV","440":"LT","442":"LU","470":"MT","528":"NL","616":"PL","620":"PT","642":"RO","703":"SK","705":"SI","724":"ES","752":"SE","578":"NO","756":"CH","826":"GB"};
const W = 800, H = 760;
// rzut stożkowy wiernokątny dla Europy; dopasowany do prostokąta Europy kontynentalnej
const proj = geoConicConformal().parallels([40, 65]).rotate([-12, 0]);
const box = {type: "Feature", geometry: {type: "Polygon", coordinates: [[[-11, 34.5], [-11, 71.5], [33, 71.5], [33, 34.5], [-11, 34.5]].reverse()]}};
proj.fitExtent([[6, 6], [W - 6, H - 6]], {type: "MultiPoint", coordinates: [[-10.5, 36], [-10, 60], [31.5, 70.5], [34, 35], [-24, 64]]});
proj.clipExtent([[0, 0], [W, H]]);
const path = geoPath(proj).digits(1);
const out = {w: W, h: H, countries: {}, bg: []};
for (const f of fc.features) {
  const d = path(f);
  if (!d) continue;
  const code = ISO[f.id];
  if (code) out.countries[code] = d;
  else out.bg.push(d);
}
// punkt etykiety (centroid największego wielokąta)
out.labels = {};
for (const f of fc.features) {
  const code = ISO[f.id]; if (!code) continue;
  let g = f.geometry, best = null, ba = -1;
  const polys = g.type === "MultiPolygon" ? g.coordinates.map(c => ({type: "Polygon", coordinates: c})) : [g];
  for (const p of polys) { const a = path.area(p); if (a > ba) { ba = a; best = p; } }
  const c = path.centroid(best); out.labels[code] = [Math.round(c[0]), Math.round(c[1]), Math.round(ba)];
}
// sąsiedzi poza obszarem: zostaw tylko ścieżki w obszarze
delete out.labels.MT;
fs.writeFileSync("mixtracker/europe_map.json", JSON.stringify(out));
console.log(Object.keys(out.countries).length, out.bg.length, (JSON.stringify(out).length/1024).toFixed(0)+" KB");
