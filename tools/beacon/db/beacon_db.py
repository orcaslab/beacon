#!/usr/bin/env python3
# Copyright © 2019-2026
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""beacon_db -- Beacon's path database: every Vivado result becomes labelled
data, and Beacon's timing and locality terms are re-fitted from it.

  ingest   one export of xilinx_path_export.tcl (beside this script) into the database:
           per path its label (requirement - slack) and its pre-placement
           features (levels, fanout, the floorplan's region of each end, the
           span of the hierarchy subtree that holds both ends)
  list     the designs in the database
  fit      fit a model on every design (or all but --holdout) -> JSON
  report   held-out accuracy per design (each design predicted by a fit on the
           others): Spearman vs slack, precision/recall of failing end points,
           predicted Fmax error, against today's Beacon (levels, and the 11-07
           calibrated terms) -> markdown
  design   design-level locality and routability features of each design

  beacon_db.py ingest <export dir> --db DB --family fsd --stage routed --outcome routed
  beacon_db.py report --db DB -o beacon_accuracy.md

Models (all linear in ns; features computable before placement):
  levels  Beacon today: logic levels (rank), budget 3.6 levels per ns (18 at 5 ns)
  v0      Beacon's 11-07 calibrated terms (calibrations/v80_timing.json): levels,
          log2 fanout, SLR crossings (taken from the floorplan's regions)
  v1      v0's terms plus the physical locality terms: span of the end points'
          common hierarchy subtree (sqrt of its LUTs, a diameter), fanout area
          (sqrt fanout), crossings between floorplan regions, an unregistered
          crossing flag, ends the floorplan leaves free (times span), and the
          region fill (times span); fitted by least squares, each design
          weighing the same.
"""
import argparse, collections, csv, gzip, json, math, os, re, sys, time

SCHEMA = 1
PATH_COLS = ['design', 'clock', 'req', 'slack', 'delay', 'dp', 'logic', 'net', 'skew', 'unc', 'levels', 'maxfo',
             'nfo1k', 'slr_changes', 'length', 'bbox', 's_slr', 'e_slr', 's_cr', 'e_cr', 's_col', 's_row', 'e_col',
             'e_row', 's_ref', 'e_ref', 's_cell', 'e_cell', 'sample', 'weight',
             # pre-placement features
             's_reg', 'e_reg', 'x_reg', 'free', 'lca', 'lca_luts', 'span', 'xdist']
DESIGN_COLS = ['design', 'family', 'kind', 'stage', 'outcome', 'overlaps', 'clock', 'period', 'wns', 'fmax', 'rows',
               'luts', 'regions', 'asg_fill', 'pre_fill', 'placed_fill', 'blob', 'ingested', 'source', 'note',
               'congestion']

# V80 dynamic region per SLR (calibrations/v80_slr.json, Vivado 2025.2 site count of pblock_slash).
V80_SLR_LUTS = {'SLR0': 261120, 'SLR1': 665952, 'SLR2': 707808}
V80_SLR_LUTRAM = {'SLR0': 130560, 'SLR1': 332976, 'SLR2': 353904}  # SLICEM LUTs


def die(msg):
    sys.exit(f'beacon_db: {msg}')


def opener(path, mode='rt'):
    return gzip.open(path, mode) if path.endswith('.gz') else open(path, mode)


# ---------------------------------------------------------------------------------------------------- readers

def parse_util_hier(path):
    """report_utilization -hierarchical: {instance path below the top: (LUTs, FFs, LUTRAM, RAMB36, URAM)}, top LUTs.
    Indentation (2 spaces a level) gives the tree; '(name)' rows (a module's own cells) are skipped."""
    tree, stack, cols, top = {}, [], None, None
    with opener(path) as f:
        for line in f:
            if not line.startswith('|'):
                continue
            cells = line.rstrip('\n').split('|')[1:-1]
            if cols is None:
                if cells and cells[0].strip() == 'Instance':
                    cols = [c.strip() for c in cells]
                continue
            name = cells[0]
            ind = (len(name) - len(name.lstrip(' '))) // 2
            name = name.strip()
            if name.startswith('(') or not name:
                continue
            vals = dict(zip(cols, (c.strip() for c in cells)))
            def num(k):
                try:
                    return float(vals.get(k, '0') or 0)
                except ValueError:
                    return 0.0
            rec = (num('Total LUTs'), num('FFs'), num('LUTRAMs'), num('RAMB36'), num('URAM'))
            if ind == 0:
                top = rec
                tree['\0top_lutram'] = rec[2]
                stack = []
                continue
            stack = stack[:ind - 1] + [name]
            tree['/'.join(stack)] = rec
    return tree, top


def load_assign(path, cr_slr=None):
    """The floorplan the placer was given: {hierarchical cell: region}. USER_SLR_ASSIGNMENT gives the SLR; a pblock
    counts as a region only when its clock-region ranges fall in one SLR (cr_slr: clock-region row -> SLR, learned from
    the placed end points), so a design-wide pblock (the shell's dynamic region) leaves its cells free."""
    reg = {}
    if not os.path.exists(path):
        return reg
    pb_slr = {}
    for r in csv.DictReader(opener(path), delimiter='\t'):
        if r['kind'] == 'slr':
            reg[r['cell']] = r['value'].strip()
        elif r['kind'] == 'pblock' and r['cell'] not in reg:
            f = r['value'].split()
            name, ranges = f[0], (f[1] if len(f) > 1 else '')
            if name not in pb_slr:
                slrs = set()
                for rg in ranges.split(','):
                    m = re.fullmatch(r'CLOCKREGION_X\d+Y(\d+):CLOCKREGION_X\d+Y(\d+)', rg)
                    if m and cr_slr:
                        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
                        slrs |= {cr_slr[y] for y in range(lo, hi + 1) if y in cr_slr}
                    elif re.fullmatch(r'SLR\d+', rg):
                        slrs.add(rg)
                    else:
                        slrs.add('?')
                pb_slr[name] = next(iter(slrs)) if len(slrs) == 1 and '?' not in slrs else ''
            if pb_slr[name]:
                reg[r['cell']] = pb_slr[name]
    return reg


def read_floorplan_tcl(path):
    """A flow's floorplan Tcl as {cell or 're:<regex>': SLR}: `set c [get_cells ... {X}]` (with -regexp, a pattern
    over full hierarchical names) followed by `set_property USER_SLR_ASSIGNMENT SLRn $c`, or the property set on a
    `[get_cells X]` directly. The DCP export (assign.tsv) has the resolved cells; this is the pre-Vivado form."""
    reg, cur = {}, None
    gc = re.compile(r'get_cells\s+([^\]]*)\]')
    for line in open(path):
        line = line.strip()
        if line.startswith('#'):
            continue
        m = re.match(r'set\s+(\w+)\s+\[get_cells\s+(.*)\]\s*$', line)
        if m:
            args = m.group(2)
            pat = re.search(r'\{([^}]*)\}\s*$', args)
            name = pat.group(1) if pat else args.split()[-1]
            if '-regexp' in args:
                cur = 're:' + name
            elif '-hierarchical' in args:  # a leaf name at any depth
                cur = 're:(.*/)?' + re.escape(name)
            else:
                cur = name
            continue
        m = re.search(r'USER_SLR_ASSIGNMENT\s+(\S+)\s+(\$\w+|\[get_cells[^\]]*\])', line)
        if m:
            tgt = m.group(2)
            if tgt.startswith('$'):
                if cur:
                    reg[cur] = m.group(1)
            else:
                g = gc.search(tgt)
                for c in (g.group(1).replace('-quiet', '').replace('-hierarchical', '').split() if g else []):
                    reg[c.strip('{}')] = m.group(1)
    return reg


def prefixes(cell):
    parts = cell.split('/')
    for i in range(len(parts), 0, -1):
        yield '/'.join(parts[:i])


def region_of(cell, reg, cache):
    """The floorplan region of a cell: its own or its nearest assigned ancestor's ('re:' keys match a full name)."""
    if cell in cache:
        return cache[cell]
    pats = cache.setdefault('\0re', [(re.compile(k[3:] + '$'), v) for k, v in reg.items() if k.startswith('re:')])
    r = ''
    for p in prefixes(cell):
        if p in reg:
            r = reg[p]
            break
        hit = next((v for rx, v in pats if rx.match(p)), None)
        if hit:
            r = hit
            break
    cache[cell] = r
    return r


def lca(a, b):
    pa, pb = a.split('/')[:-1], b.split('/')[:-1]  # parents (the cells themselves are leaves)
    n = 0
    for x, y in zip(pa, pb):
        if x != y:
            break
        n += 1
    return '/'.join(pa[:n])


def subtree_luts(inst, tree, top_luts, cache):
    if inst in cache:
        return cache[inst]
    v = top_luts
    if inst:
        for p in prefixes(inst):
            if p in tree:
                v = tree[p][0]
                break
    cache[inst] = v
    return v


def slr_num(r):
    m = re.fullmatch(r'SLR(\d+)', r or '')
    return int(m.group(1)) if m else None


def region_cross(a, b):
    """Crossings between two floorplan regions: |SLR difference|, 1 for different pblocks, 0 if either is free."""
    if not a or not b or a == b:
        return 0
    na, nb = slr_num(a), slr_num(b)
    return abs(na - nb) if na is not None and nb is not None else 1


def slr_plan(reg, tree, top_luts):
    """The floorplan as SLRs, and what no placement can avoid. A region named SLRn is that SLR; any other
    USER_SLR_ASSIGNMENT value is a group the placer puts in one SLR of its choice, mapped here largest group first to
    the SLR with the most room left. Returns (reg with every region as an SLR name, pre_fill, blob, fill per SLR):
    pre_fill = the larger of the fullest SLR's assigned LUTs and the whole design spread evenly; blob = the largest
    subtree pinned whole, as a share of the SLR it lands in (more than the placement fill straddles)."""
    top_of = {}
    for c, r in reg.items():
        if not any(p in reg for p in list(prefixes(c))[1:]) and c in tree:
            top_of[c] = r
    load = collections.Counter()
    for c, r in top_of.items():
        load[r] += tree[c][0]
    room = dict(V80_SLR_LUTS)
    for r, v in load.items():
        if r in room:
            room[r] -= v
    gmap = {}
    for r, v in sorted(load.items(), key=lambda kv: -kv[1]):
        if r in V80_SLR_LUTS or r.startswith('pblock:'):
            continue
        s_ = max(room, key=room.get)
        gmap[r] = s_
        room[s_] -= v
    reg2 = {c: gmap.get(r, r) for c, r in reg.items()}
    fill = collections.Counter()
    for r, v in load.items():
        s_ = gmap.get(r, r)
        if s_ in V80_SLR_LUTS:
            fill[s_] += v / V80_SLR_LUTS[s_]
    total = top_luts / sum(V80_SLR_LUTS.values()) if top_luts else 0.0
    pre = max([total] + list(fill.values()))
    # LUTRAM needs SLICEM sites, half of the LUTs: an SLR's assigned LUTRAM against them, and the design's.
    lram = collections.Counter()
    for c, r in top_of.items():
        s_ = gmap.get(r, r)
        if s_ in V80_SLR_LUTRAM:
            lram[s_] += tree[c][2] / V80_SLR_LUTRAM[s_]
    top_lram = tree.get('\0top_lutram', 0.0)
    pre_lram = max([top_lram / sum(V80_SLR_LUTRAM.values())] + list(lram.values()))
    blob = max([max(tree[c][0] / V80_SLR_LUTS[gmap.get(r, r)], tree[c][2] / V80_SLR_LUTRAM[gmap.get(r, r)])
                for c, r in top_of.items() if gmap.get(r, r) in V80_SLR_LUTS] or [0.0])
    fill = dict(fill)
    fill['lutram'] = pre_lram
    return reg2, max(pre, pre_lram), blob, fill


NODE_DEPTH = 7


def node_of(cell, depth=NODE_DEPTH):
    """The instance a cell is placed with in the coarse placement: its parent, cut to `depth` levels."""
    return '/'.join(cell.split('/')[:-1][:depth])


# Hard blocks the shell places before the fabric placer runs (NoC, HBM and DDR controllers, transceivers, the
# processing system, I/O): their sites are known before placement, so the coarse placement anchors them.
HARD_REF = re.compile(r'^(NOC_|HBM|DDRMC|GT|XPIO|XPHY|PS9|PS_|CIPS|AIE|PCIE|CPM|MMCM|DPLL|XPLL)')


def coarse_slr_positions(edges, reg, n=None, fixed=None):
    """1-D quadratic placement on the SLR axis: nodes are instances (node_of), edges the connections (start, end,
    weight); instances in a floorplan region are anchored at their SLR's index, the rest solve the Laplacian system
    (free components with no anchor sit at the middle SLR, 1.0). Returns {node: position}."""
    N = (n or np)()
    idx, anchor = {}, {}
    rc = {}
    fixed = fixed or {}
    def nid(c):
        k = node_of(c)
        if k not in idx:
            idx[k] = len(idx)
            r = region_of(k + '/x', reg, rc) if k else ''
            m = slr_num(r)
            if m is None and k in fixed:
                m = fixed[k]
            if m is not None:
                anchor[idx[k]] = float(m)
        return idx[k]
    E = collections.Counter()
    for s, e, w in edges:
        i, j = nid(s), nid(e)
        if i != j:
            E[(min(i, j), max(i, j))] += w
    n_ = len(idx)
    if not n_:
        return {}
    free = [i for i in range(n_) if i not in anchor]
    if not free:
        return {k: anchor[i] for k, i in idx.items()}
    fi = {i: t for t, i in enumerate(free)}
    L = N.zeros((len(free), len(free)))
    b = N.zeros(len(free))
    for (i, j), w in E.items():
        for u, v in ((i, j), (j, i)):
            if u in fi:
                L[fi[u], fi[u]] += w
                if v in fi:
                    L[fi[u], fi[v]] -= w
                else:
                    b[fi[u]] += w * anchor[v]
    # A weak pull to the middle SLR keeps components without anchors (and the system) well posed.
    eps = 1e-3 * (L.diagonal().mean() if len(free) else 1.0) + 1e-9
    L += eps * N.eye(len(free))
    b += eps * 1.0
    y = N.linalg.solve(L, b)
    out = {k: anchor.get(i, None) for k, i in idx.items()}
    for k, i in idx.items():
        if out[k] is None:
            out[k] = float(y[fi[i]])
    return out


def parse_place_congestion(path):
    """The placer's final estimated congestion (the last 'Post_DP Estimated Congestion' device table of a place_design
    log): the largest congested window per type as a Vivado level (log2 of the window's side), {global, long, short}."""
    txt = opener(path).read() if os.path.exists(path) else ''
    i = txt.rfind('Post_DP Estimated Congestion')
    if i < 0:
        return {}
    out = {'global': 0, 'long': 0, 'short': 0}
    for line in txt[i:i + 4000].splitlines()[5:]:
        m = re.match(r'\|\s*(NORTH|SOUTH|EAST|WEST)\|\s*(\d+)x\d+\|[^|]*\|\s*(\d+)x\d+\|[^|]*\|\s*(\d+)x\d+\|', line)
        if m:
            for k, v in zip(('global', 'long', 'short'), m.groups()[1:]):
                out[k] = max(out[k], int(math.log2(int(v))))
        elif line.startswith('Total net WL'):
            break
    return out


def parse_rda_congestion(path):
    """report_design_analysis -congestion: the largest level per type ({global, long, short})."""
    out = {}
    if not os.path.exists(path):
        return out
    for line in opener(path):
        c = [x.strip() for x in line.split('|')]
        if len(c) > 4 and c[1] in ('North', 'South', 'East', 'West') and c[3].isdigit():
            k = c[2].lower()
            out[k] = max(out.get(k, 0), int(c[3]))
    return out


CONGESTION_RISK = 5  # Vivado's guidance (UG949): a congestion level of 5 or more is a routing risk


def cmd_score(a):
    """Post-placement mode for one placed design already in the database (its export ingested): the routed Fmax
    predicted by v1-post fitted on every other design (the label stage set to routed), the end points predicted to
    fail, the hierarchy edges behind them, and the routability flag from the placer's congestion (a level >= 5 is a
    risk). Writes the summary to --out too. Meant to run between placement and routing."""
    N = np()
    designs = load_designs(a.db)
    d = dict(designs[a.design])
    others = [k for k in designs if k != a.design and fit_ok(designs[k])]
    m = fit_v1(a.db, designs, others, mode='post')
    P = load_paths(a.db, a.design)
    d['stage'] = 'routed'
    pred = predict(m, P, d)
    sel = P['main'] == d['clock']
    crit = crit_delay(pred[sel], P['weight'][sel])
    period = fnum(d['period'])
    nfail = float((P['weight'][sel] * ((P['req'][sel] - pred[sel]) < 0)).sum())
    cong = dict(kv.split(':') for kv in (d.get('congestion') or '').split()) if d.get('congestion') else {}
    lv = max([int(v) for v in cong.values()] or [0])
    risk = 'RISK' if lv >= CONGESTION_RISK else ('ok' if cong else 'unknown (no place log)')
    lines = [f'a32-BP Beacon v1 post-placement prediction for {a.design} (fit on {len(m["designs"])} other designs, '
             f'{time.strftime("%Y-%m-%d %H:%M")}) [model]',
             f'predicted routed Fmax {1000 / crit:.1f} MHz (critical delay {crit:.2f} ns, 100th worst end point) '
             f'against the {period:g} ns clock ({1000 / period:.1f} MHz): {"MEETS" if crit <= period else "MISSES"}; '
             f'~{nfail:,.0f} end points predicted to fail; placed estimate today {fnum(d["fmax"]):.1f} MHz',
             f'routability: {risk} (placer congestion {d.get("congestion") or "-"}; level >= {CONGESTION_RISK} = risk; '
             f'held-out precision of this flag is low, see the accuracy report)',
             f'held-out accuracy of v1-post: see the latest report (beacon_db.py report)']
    txt = '\n'.join(lines)
    print(txt)
    if a.out:
        open(a.out, 'w').write(txt + '\n')


def cmd_congestion(a):
    """Record a design's placement congestion (from its place_design log or a report_design_analysis -congestion
    report) in designs.tsv: the post-placement routability features."""
    designs = load_designs(a.db)
    c = parse_rda_congestion(a.report) if a.report.endswith('.rpt') else parse_place_congestion(a.report)
    if not c:
        die(f'{a.report}: no congestion table')
    d = designs[a.design]
    d['congestion'] = ' '.join(f'{k}:{c.get(k, 0)}' for k in ('global', 'long', 'short'))
    save_designs(a.db, designs)
    print(f'{a.design}: congestion {d["congestion"]} ({a.report})')


def parse_util_slr(path):
    """report_utilization -slr: placed LUT share per SLR (CLB LUTs row), or {}."""
    out = {}
    if not os.path.exists(path):
        return out
    txt = open(path).read()
    m = re.search(r'SLR CLB Logic and Dedicated Block Utilization.*?\n(\+.*?)\n\n', txt, re.S)
    if not m:
        return out
    hdr = None
    for line in m.group(1).splitlines():
        if not line.startswith('|'):
            continue
        c = [x.strip() for x in line.split('|')[1:-1]]
        if hdr is None:
            hdr = c
            continue
        if c[0] == 'CLB LUTs':
            for k, v in zip(hdr[1:], c[1:]):
                if re.fullmatch(r'SLR\d+', k):
                    try:
                        out[k] = float(v)
                    except ValueError:
                        pass
    return out


# ---------------------------------------------------------------------------------------------------- ingest

def fnum(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def cmd_ingest(a):
    src = a.export
    meta = {}
    clocks = {}
    for line in opener(os.path.join(src, 'meta.tsv')):
        f = line.rstrip('\n').split('\t')
        if f[0] == 'clock':
            clocks[f[1]] = (fnum(f[2]), fnum(f[3]))
        else:
            meta[f[0]] = f[1] if len(f) > 1 else ''
    did = a.id or meta.get('design')
    pth = os.path.join(src, 'paths.tsv')
    if not os.path.exists(pth):
        pth += '.gz'
    rows = list(csv.DictReader(opener(pth), delimiter='\t'))
    uh = os.path.join(src, 'util_hier.rpt')
    tree, top = parse_util_hier(uh if os.path.exists(uh) else uh + '.gz') if (os.path.exists(uh) or os.path.exists(uh + '.gz')) else ({}, None)
    top_luts = top[0] if top else 0.0
    cr_slr = {}
    for r in rows:
        for c, k in (('s_cr', 's_slr'), ('e_cr', 'e_slr')):
            m = re.fullmatch(r'X\d+Y(\d+)', r.get(c) or '')
            if m and r.get(k):
                cr_slr[int(m.group(1))] = 'SLR' + r[k]
    reg = load_assign(os.path.join(src, 'assign.tsv'), cr_slr)
    if a.floorplan:
        for fp in a.floorplan:
            reg.update(read_floorplan_tcl(fp))
    reg, pre, blob, asg = slr_plan(reg, tree, top_luts)
    rc, lc = {}, {}
    out = []
    w_by_clk = collections.Counter()
    for r in rows:
        req, sl = fnum(r['req']), fnum(r['slack'])
        s, e = r['s_cell'], r['e_cell']
        sr, er = region_of(s, reg, rc), region_of(e, reg, rc)
        L = lca(s, e)
        luts = subtree_luts(L, tree, top_luts, lc)
        rec = dict(r)
        rec.update(design=did, delay=f'{req - sl:.4f}', s_reg=sr, e_reg=er, x_reg=region_cross(sr, er),
                   free=int(not sr) + int(not er), lca=L, lca_luts=int(luts), span=f'{math.sqrt(max(luts, 1.0) / 1e3):.4f}')
        out.append(rec)
        w_by_clk[r['clock']] += fnum(r['weight'], 1)
    # Coarse placement along the SLR axis (pre-placement): each end's instance gets the position that minimizes the
    # squared distance to what it connects to, the floorplan's assigned instances held at their SLR.
    fixed = {}
    for r in out:
        for c, ref, sl_ in (('s_cell', 's_ref', 's_slr'), ('e_cell', 'e_ref', 'e_slr')):
            if HARD_REF.match(r.get(ref) or '') and (r.get(sl_) or '').isdigit():
                fixed[node_of(r[c])] = int(r[sl_])
    pos = coarse_slr_positions([(r['s_cell'], r['e_cell'], fnum(r['weight'], 1)) for r in out
                                if r['sample'].startswith('band')] or
                               [(r['s_cell'], r['e_cell'], 1.0) for r in out], reg, fixed=fixed)
    for rec in out:
        rec['xdist'] = f"{abs(pos.get(node_of(rec['s_cell']), 1.0) - pos.get(node_of(rec['e_cell']), 1.0)):.4f}"
    # Main clock: the one with the most end points sampled (by weight).
    main = w_by_clk.most_common(1)[0][0] if w_by_clk else ''
    period, wns = clocks.get(main, (0.0, 0.0))
    placed = parse_util_slr(os.path.join(src, 'util_slr.rpt'))
    placed_fill = max(placed.values()) / 100 if placed else ''
    db = a.db
    os.makedirs(os.path.join(db, 'paths'), exist_ok=True)
    with gzip.open(os.path.join(db, 'paths', f'{did}.tsv.gz'), 'wt') as f:
        w = csv.DictWriter(f, PATH_COLS, delimiter='\t', extrasaction='ignore')
        w.writeheader()
        w.writerows(out)
    fmax = 1000.0 / (period - min(wns, 0.0)) if period else 0.0
    overlaps = a.overlaps
    rs = os.path.join(src, 'route_status.rpt')
    if not overlaps and a.stage in ('routed', 'routed_error') and os.path.exists(rs):
        m = re.search(r'# of nets with routing errors\.*\s*:\s*(\d+)', open(rs).read())
        overlaps = f'{m.group(1)} nets with errors' if m and int(m.group(1)) else ''
    d = dict(design=did, family=a.family, kind=a.kind, stage=a.stage, outcome=a.outcome, overlaps=overlaps or '',
             clock=main, period=period, wns=wns, fmax=f'{fmax:.2f}', rows=len(out), luts=int(top_luts),
             regions=len(set(reg.values())), asg_fill=' '.join(f'{k}:{v:.2f}' for k, v in sorted(asg.items())), pre_fill=f'{pre:.3f}',
             placed_fill=placed_fill if placed_fill == '' else f'{placed_fill:.3f}', blob=f'{blob:.3f}',
             ingested=time.strftime('%Y-%m-%d %H:%M'), source=os.path.abspath(src), note=a.note or '')
    designs = load_designs(db)
    d['congestion'] = designs.get(did, {}).get('congestion', '')
    designs[did] = d
    save_designs(db, designs)
    print(f'{did}: {len(out)} paths, main clock {main} {period} ns WNS {wns} ({fmax:.1f} MHz), '
          f'{len(reg)} floorplan cells in {d["regions"]} regions, pre-placement fill {pre:.2f}, blob {blob:.2f}')


def cmd_ingest_ep(a):
    """An OOC or native gate run directory: ep.csv (every failing end point: slack,levels,logic,net,start,end), its
    clock (clk.xdc), WNS (routed or placed timing summary) and hierarchy (routed_util_hier.rpt). No fanout, no
    placement, no passing end points: the rows are marked sample=failing and the fit leaves such designs out."""
    src = a.run
    did = a.id or os.path.basename(os.path.normpath(src))
    period = 0.0
    for line in open(os.path.join(src, 'clk.xdc')):
        m = re.search(r'create_clock.*-period\s+([\d.]+)', line)
        if m:
            period = float(m.group(1))
            break
    wns, stage = None, 'routed'
    for name, stg in (('routed_timing_summary.rpt', 'routed'), ('placed_timing_summary.rpt', 'placed')):
        f = os.path.join(src, name)
        if os.path.exists(f):
            t = open(f).read()
            m = re.search(r'WNS\(ns\).*?\n\s*-+.*?\n\s*(-?[\d.]+)', t)
            if m:
                wns, stage = float(m.group(1)), stg
                break
    if wns is None or not period:
        die(f'{src}: no clock period or timing summary')
    tree, top = ({}, None)
    for name in ('routed_util_hier.rpt', 'synth_util_hier.rpt'):
        f = os.path.join(src, name)
        if os.path.exists(f):
            tree, top = parse_util_hier(f)
            break
    top_luts = top[0] if top else 0.0
    lc = {}
    out = []
    for r in csv.reader(open(os.path.join(src, 'ep.csv'))):
        if not r or r[0] == 'slack':
            continue
        sl, lv, lg, nt, sp, ep = r[:6]
        s, e = sp.rsplit('/', 1)[0], ep.rsplit('/', 1)[0]
        L = lca(s, e)
        luts = subtree_luts(L, tree, top_luts, lc)
        out.append(dict(design=did, clock='main', req=period, slack=sl, delay=f'{period - fnum(sl):.4f}',
                        dp=f'{fnum(lg) + fnum(nt):.4f}', logic=lg, net=nt, levels=lv, maxfo='', s_cell=s, e_cell=e,
                        sample='failing', weight=1, s_reg='', e_reg='', x_reg=0, free=2, lca=L, lca_luts=int(luts),
                        span=f'{math.sqrt(max(luts, 1.0) / 1e3):.4f}'))
    db = a.db
    os.makedirs(os.path.join(db, 'paths'), exist_ok=True)
    with gzip.open(os.path.join(db, 'paths', f'{did}.tsv.gz'), 'wt') as f:
        w = csv.DictWriter(f, PATH_COLS, delimiter='\t', extrasaction='ignore')
        w.writeheader()
        w.writerows(out)
    fmax = 1000.0 / (period - min(wns, 0.0))
    d = dict(design=did, family=a.family, kind=a.kind, stage=stage, outcome='routed' if stage == 'routed' else 'placed',
             overlaps='', clock='main', period=period, wns=wns, fmax=f'{fmax:.2f}', rows=len(out), luts=int(top_luts),
             regions=0, asg_fill='', pre_fill=f'{top_luts / sum(V80_SLR_LUTS.values()):.3f}', placed_fill='', blob='',
             ingested=time.strftime('%Y-%m-%d %H:%M'), source=os.path.abspath(src), note='ep.csv: failing only')
    designs = load_designs(db)
    designs[did] = d
    save_designs(db, designs)
    print(f'{did}: {len(out)} failing end points, {period} ns WNS {wns} ({fmax:.1f} MHz), {top_luts:.0f} LUTs')


def load_designs(db):
    p = os.path.join(db, 'designs.tsv')
    if not os.path.exists(p):
        return {}
    return {r['design']: r for r in csv.DictReader(open(p), delimiter='\t')}


def save_designs(db, designs):
    p = os.path.join(db, 'designs.tsv')
    with open(p + '.new', 'w') as f:
        w = csv.DictWriter(f, DESIGN_COLS, delimiter='\t', extrasaction='ignore')
        w.writeheader()
        for k in sorted(designs):
            w.writerow(designs[k])
    os.replace(p + '.new', p)


def cmd_list(a):
    ds = load_designs(a.db)
    print('\t'.join(DESIGN_COLS[:15]))
    for d in ds.values():
        print('\t'.join(str(d.get(k, '')) for k in DESIGN_COLS[:15]))


# ---------------------------------------------------------------------------------------------------- models

def np():
    try:
        import numpy
        return numpy
    except ImportError:
        die('numpy is required for fit/report')


V0 = dict(intercept_ns=2.28841621610982, ns_per_level=0.3511982954550092, ns_per_log2_fanout=0.05002684972327406,
          ns_slr_crossing=1.0683768173858161)  # calibrations/v80_timing.json (fcb7029, 11-07)

V1_TERMS = ['1', 'levels', 'log2_fanout', 'sqrt_fanout', 'span', 'x_reg', 'unreg_x', 'free_span', 'fill_span',
            'estimate', 'coarse_slr_dist', 'requirement']
POST_TERMS = V1_TERMS + ['placed_slr_dist', 'placed_tile_dist']  # post-placement mode: the ends as placed


def load_paths(db, did, n=np):
    N = n()
    rows = list(csv.DictReader(gzip.open(os.path.join(db, 'paths', f'{did}.tsv.gz'), 'rt'), delimiter='\t'))
    rows = [r for r in rows if 0 < fnum(r['req']) <= 20]
    g = lambda k, d=0.0: N.array([fnum(r.get(k), d) for r in rows], dtype=float)
    P = dict(req=g('req'), slack=g('slack'), delay=g('delay'), levels=g('levels'), maxfo=g('maxfo'),
             slr_changes=g('slr_changes'), x_reg=g('x_reg'), free=g('free'), span=g('span'), weight=g('weight', 1),
             xdist=g('xdist'),
             main=N.array([r['clock'] for r in rows]), band=N.array([r['sample'].startswith('band') for r in rows]))
    # Placement of the two ends (post-placement mode): SLR distance and Manhattan tile distance.
    sl, el = g('s_slr', -1), g('e_slr', -1)
    P['dslr'] = N.where((sl >= 0) & (el >= 0), N.abs(sl - el), 0.0)
    P['dist'] = (N.abs(g('s_col') - g('e_col')) + N.abs(g('s_row') - g('e_row'))) / 100.0
    return P


def features(P, d, n=np, post=False):
    N = n()
    fo = N.maximum(P['maxfo'], 1.0)
    fill = fnum(d.get('pre_fill'))
    est = 1.0 if d['stage'] in ('placed', 'routed_error', 'synth') else 0.0
    unreg = ((P['x_reg'] > 0) & (P['levels'] >= 1)).astype(float)
    cols = [N.ones_like(fo), P['levels'], N.log2(fo), N.sqrt(fo) / 32.0, P['span'], P['x_reg'], unreg,
            P['free'] * P['span'] / 2.0, fill * P['span'], N.full_like(fo, est), P['xdist'], P['req']]
    if post:
        cols += [P['dslr'], P['dist']]
    return N.stack(cols, axis=1)


def predict(model, P, d, n=np):
    N = n()
    if model == 'levels':
        return P['levels'] / 3.6
    if model == 'v0':
        fo = N.maximum(P['maxfo'], 1.0)
        return V0['intercept_ns'] + V0['ns_per_level'] * P['levels'] + V0['ns_per_log2_fanout'] * N.log2(fo) + \
            V0['ns_slr_crossing'] * P['x_reg']
    if model.get('mode') == 'res':
        X = features(P, d, n)[:, [V1_TERMS.index(t) for t in model['terms']]]
        return predict('v0', P, d, n) + X @ N.array(model['coef'])
    return features(P, d, n, post=model.get('mode') == 'post') @ N.array(model['coef'])


def fit_ok(d):
    """Designs with a sample over the slack range (not failing end points only) are fitted."""
    return not d.get('note', '').startswith('ep.csv')


_PATHS = {}


def paths_cached(db, did):
    if (db, did) not in _PATHS:
        _PATHS[(db, did)] = load_paths(db, did)
    return _PATHS[(db, did)]


def fit_v1(db, designs, ids, ridge=1e-3, n=np, drop=(), mode='pre'):
    """Least squares of requirement - slack on V1_TERMS (less `drop`), each design weighing the same."""
    N = n()
    Xs, ys, ws = [], [], []
    ids = [i for i in ids if fit_ok(designs[i])]
    for did in ids:
        P = paths_cached(db, did)
        if not len(P['delay']):
            continue
        X = features(P, designs[did], post=mode == 'post')
        w = P['weight'] / P['weight'].sum()  # each design weighs the same
        Xs.append(X), ys.append(P['delay']), ws.append(w)
    X, y, w = N.concatenate(Xs), N.concatenate(ys), N.concatenate(ws)
    sw = N.sqrt(w)[:, None]
    A = X * sw
    k = A.shape[1]
    # Drop terms with no variation in the training designs (e.g. no floorplan anywhere): zero coefficient.
    terms = POST_TERMS if mode == 'post' else V1_TERMS
    live = N.array([(j == 0 or N.ptp(X[:, j]) > 0) and terms[j] not in drop for j in range(k)])
    coef = N.zeros(k)
    Al = A[:, live]
    coef[live] = N.linalg.solve(Al.T @ Al + ridge * N.eye(live.sum()), Al.T @ (y * sw[:, 0]))
    res = y - X @ coef
    return dict(terms=terms, mode=mode, coef=coef.tolist(), designs=list(ids),
                rmse_ns=float(N.sqrt((w * res * res).sum() / w.sum())))


RES_TERMS = ['1', 'requirement', 'coarse_slr_dist', 'free_span', 'fill_span']


def fit_res(db, designs, ids, ridge=1e-3, n=np):
    """v1 before placement, residual form (the survey's recommendation): v0's calibrated physics (levels, fanout,
    crossings) kept as is, and a least-squares correction of its error on the locality terms (clock requirement,
    coarse SLR distance, free ends x span, region fill x span), each design weighing the same."""
    N = n()
    ids = [i for i in ids if fit_ok(designs[i])]
    cols = [V1_TERMS.index(t) for t in RES_TERMS]
    Xs, ys, ws = [], [], []
    for did in ids:
        P = paths_cached(db, did)
        if not len(P['delay']):
            continue
        Xs.append(features(P, designs[did])[:, cols])
        ys.append(P['delay'] - predict('v0', P, designs[did]))
        ws.append(P['weight'] / P['weight'].sum())
    X, y, w = N.concatenate(Xs), N.concatenate(ys), N.concatenate(ws)
    sw = N.sqrt(w)[:, None]
    A = X * sw
    R = ridge * N.eye(len(cols))
    R[0, 0] = 0.0
    coef = N.linalg.solve(A.T @ A + R, A.T @ (y * sw[:, 0]))
    return dict(terms=RES_TERMS, mode='res', coef=coef.tolist(), designs=list(ids))


def spearman(a, b, n=np):
    N = n()
    if len(a) < 3:
        return float('nan')
    def rank(x):
        o = N.argsort(x, kind='mergesort')
        r = N.empty(len(x))
        r[o] = N.arange(len(x))
        # average ties
        _, inv, cnt = N.unique(x, return_inverse=True, return_counts=True)
        s = N.zeros(len(cnt))
        N.add.at(s, inv, r)
        return (s / cnt)[inv]
    ra, rb = rank(a), rank(b)
    if ra.std() == 0 or rb.std() == 0:
        return float('nan')
    return float(N.corrcoef(ra, rb)[0, 1])


FMAX_TOP = 100


def crit_delay(pred, w, top=None):
    """The design's critical delay from per-end-point predictions: the predicted delay of the `top`-th worst end point,
    counting each sampled end point by its weight (FMAX_TOP = 100). Not the single worst: Vivado's timing-driven
    placement and phys_opt work hardest on the extreme few (replication, priority), so the period is set by the
    population, and a mean model's single largest prediction is its largest error."""
    N = np()
    top = FMAX_TOP if top is None else top
    o = N.argsort(-pred)
    cw = N.cumsum(w[o])
    return float(pred[o][min(int(N.searchsorted(cw, top)), len(o) - 1)])


def evaluate(model, P, d, n=np):
    """Held-out metrics of one design: Spearman(predicted slack, slack), weighted precision/recall of failing end
    points, and the predicted Fmax of the main clock (1 / the largest predicted delay) against the measured."""
    N = n()
    pred = predict(model, P, d)
    ps = P['req'] - pred
    # Rank and classification metrics over the slack-band sample (representative of every end point); where a design
    # has none (an ep.csv run: failing end points only), over what it has.
    b = P['band'] if P['band'].any() else N.ones(len(ps), bool)
    out = dict(rho=spearman(ps[b], P['slack'][b]))
    fail, pf, w = P['slack'][b] < 0, ps[b] < 0, P['weight'][b]
    tp = (w * (fail & pf)).sum()
    out['precision'] = float(tp / (w * pf).sum()) if (w * pf).sum() else float('nan')
    out['recall'] = float(tp / (w * fail).sum()) if (w * fail).sum() else float('nan')
    m = P['main'] == d['clock']
    out['fmax_pred'] = float(1000.0 / max(crit_delay(pred[m], P['weight'][m]), 0.5)) if m.any() else float('nan')
    out['fmax'] = fnum(d['fmax'])
    # A design that met its clock has Fmax >= the clock: a prediction at or above it is exact.
    met = fnum(d['wns']) >= 0
    if met and out['fmax_pred'] >= out['fmax']:
        out['fmax_err'] = 0.0
    else:
        out['fmax_err'] = out['fmax_pred'] / out['fmax'] - 1 if out['fmax'] else float('nan')
    W = P['weight']
    out['bias_ns'] = float((W * (pred - P['delay'])).sum() / W.sum())
    return out


def match_key(path):
    """A register's key shared by Beacon and Vivado: its hierarchical name with Vivado's flattening (2F for '/',
    '.' and '[' as '_') and the cell suffixes (_reg[..], _replica, _fret, LUTRAM bit ranges) or Beacon's port
    (.d[..], .wd[..]) removed."""
    import importlib.util
    global _BEACON
    if '_BEACON' not in globals():
        spec = importlib.util.spec_from_file_location('beacon', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'beacon.py'))
        _BEACON = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_BEACON)
    B = _BEACON
    parts = path.replace('2F', '/').split('/')
    # Vivado: drop primitives below a memory or DSP cell
    for i, p_ in enumerate(parts):
        if (p_.startswith('Memory_reg') or re.match(r'(RAM[A-H]\w*|DP|SP|RAMD\d+_INST|RAMS\d+_INST)$', p_)
                or re.search(r'_reg(_r\d+)?_\d+_\d+_\d+_\d+$', p_) and i + 1 < len(parts) or B.DSP_SUBCELL.match(p_)):
            parts = parts[:i] if i > 0 else parts[:1]  # the memory or DSP cell is the register
            break
    leaf = re.sub(r'_rep__\d+$', '', parts[-1])
    leaf = re.sub(r'_reg(_r\d+)?_\d+_\d+_\d+_\d+$', '', leaf)
    leaf = B.VIVADO_CELL.sub('', leaf)
    leaf = B.FOLD_SUFFIX.sub('', leaf)
    leaf = re.sub(r'\.(d|wd|wa|we|wm|ra|re|ce|q|en|rst|addr|wdata|rdata)(\[\d+\])?$', '', leaf)
    leaf = re.sub(r'_(read|rdata|wdata|p\d+|rep\d*)$', '', leaf)
    return B.norm('/'.join(parts[:-1] + [leaf]))


def k_of(r):
    return match_key(r['e_cell'])


def cmd_match(a):
    """Beacon's levels for an exported design: each Vivado end point matched to the screened register of the same
    key (match_key), giving `blevels` in its path table; prints the match rate, Spearman(Beacon levels, slack) over
    the band sample and the Beacon/Vivado level ratio."""
    N = np()
    inst = {r['id']: r['instance'] for r in csv.DictReader(open(os.path.join(a.screen, 'instances.csv')))}
    pre = a.prefix.rstrip('/') + '/'
    best = {}
    with opener(os.path.join(a.screen, 'endpoints.csv')) as f:
        for r in csv.DictReader(f):
            i = inst.get(r['end_inst'], '')
            k = match_key(pre + (i + '/' if i else '') + r['end'])
            lv = int(r['levels'])
            if lv > best.get(k, -1):
                best[k] = lv
    pth = os.path.join(a.db, 'paths', f'{a.design}.tsv.gz')
    rows = list(csv.DictReader(gzip.open(pth, 'rt'), delimiter='\t'))
    hit = 0
    for r in rows:
        k = match_key(r['e_cell'])
        if k in best:
            r['blevels'] = best[k]  # a later screen (e.g. the kernel's) adds to an earlier one's matches
            hit += 1
        else:
            r.setdefault('blevels', '')
    cols = list(rows[0].keys())
    with gzip.open(pth, 'wt') as f:
        w = csv.DictWriter(f, cols, delimiter='\t')
        w.writeheader()
        w.writerows(rows)
    sim = [r for r in rows if r['e_cell'].startswith(pre)]
    m = [r for r in sim if r['blevels'] != '' and r['sample'].startswith('band') and k_of(r) in best]
    print(f'{a.design}: {len(best)} screened registers; {hit} of {len(rows)} end points matched by this screen '
          f'({sum(r["blevels"] != "" for r in sim)} of {len(sim)} inside {pre})')
    if len(m) > 2:
        bl = N.array([float(r['blevels']) for r in m])
        vl = N.array([float(r['levels']) for r in m])
        sl = N.array([float(r['slack']) for r in m])
        print(f'  band sample, matched {len(m)}: Spearman(Beacon levels, -slack) {spearman(bl, -sl):.2f}, '
              f'(Vivado levels, -slack) {spearman(vl, -sl):.2f}, (Beacon, Vivado levels) {spearman(bl, vl):.2f}; '
              f'Beacon/Vivado levels median {N.median(bl[vl > 0] / vl[vl > 0]):.2f}')


def cmd_ablate(a):
    """Held-out (leave one design out) median |Fmax error| and Spearman of v1 with each term dropped in turn."""
    N = np()
    designs = load_designs(a.db)
    fitted = [k for k in designs if fit_ok(designs[k])]
    def score(drop):
        fe, rho = [], []
        for did in fitted:
            m = fit_v1(a.db, designs, [k for k in fitted if k != did], drop=drop)
            e = evaluate(m, paths_cached(a.db, did), designs[did])
            fe.append(abs(e['fmax_err'])), rho.append(e['rho'])
        return N.median(fe), N.max(fe), N.nanmedian(rho)
    base = score(())
    print(f'{len(fitted)} designs; all terms: |Fmax err| median {100 * base[0]:.0f} % max {100 * base[1]:.0f} %, '
          f'Spearman median {base[2]:.2f}')
    for t in V1_TERMS[1:]:
        r = score((t,))
        print(f'  without {t:12s}: |Fmax err| median {100 * r[0]:3.0f} % max {100 * r[1]:3.0f} %, Spearman {r[2]:.2f}')


def cmd_fit(a):
    designs = load_designs(a.db)
    ids = [k for k in designs if k not in (a.holdout or [])]
    m = fit_res(a.db, designs, ids) if a.mode == 'res' else fit_v1(a.db, designs, ids, mode=a.mode)
    m.setdefault('rmse_ns', float('nan'))
    m.update(about=f'beacon_db v1 fit on {len(ids)} designs ({time.strftime("%Y-%m-%d %H:%M")})', schema=SCHEMA)
    json.dump(m, open(a.out, 'w'), indent=1)
    for t, c in zip(m['terms'], m['coef']):
        print(f'  {t:12s} {c:8.4f}')
    print(f'rmse {m["rmse_ns"]:.3f} ns over {len(m["designs"])} designs -> {a.out}')


def fmt(x, p=2):
    return '—' if x is None or (isinstance(x, float) and math.isnan(x)) else f'{x:.{p}f}'


def cmd_report(a):
    designs = load_designs(a.db)
    ids = sorted(designs, key=lambda k: (designs[k]['family'], k))
    if a.only:
        ids = [k for k in ids if k in a.only]
    fitted = [k for k in designs if fit_ok(designs[k])]
    full = fit_v1(a.db, designs, fitted) if fitted else None
    lines = [f'# Beacon accuracy, {time.strftime("%Y-%m-%d %H:%M")}', '',
             f'Database `{os.path.abspath(a.db)}`: {len(designs)} designs, '
             f'{sum(int(d["rows"]) for d in designs.values()):,} labelled end points (schema {SCHEMA}).', '',
             'Each design is predicted by a v1 fit on the **other** designs (leave one design out) [fit]; '
             '`levels` and `v0` are today\'s Beacon (no fit on this database). Fmax: 1 / the largest predicted '
             'delay of the main clock\'s end points, against the measured (placed estimate for placed designs) [M]. '
             'Spearman: predicted vs measured slack over the exported end points. Precision/recall: failing end '
             'points, weighted by the slack-band sample.', '',
             '| design | family | stage | measured MHz | model | Spearman | precision | recall | predicted MHz | Fmax error | bias ns |',
             '|---|---|---|---|---|---|---|---|---|---|---|']
    summ = collections.defaultdict(list)
    for did in ids:
        d = designs[did]
        P = paths_cached(a.db, did)
        if not len(P['delay']):
            continue
        others = [k for k in fitted if k != did and (a.holdout_family == 'design' or designs[k]['family'] != d['family'])]
        m1 = fit_v1(a.db, designs, others) if others else None
        m1p = fit_v1(a.db, designs, others, mode='post') if others and d['stage'] != 'synth' and not d.get('note', '').startswith('ep.csv') else None
        m1r = fit_res(a.db, designs, others) if others else None
        for name, model in (('levels', 'levels'), ('v0', 'v0'), ('v1', m1), ('v1-res', m1r), ('v1-post', m1p)):
            if model is None:
                continue
            e = evaluate(model, P, d)
            grp = ('native' if d['kind'] == 'native' else 'folded') + (' images' if fit_ok(d) else ' OOC (failing only)')
            summ[(grp, name)].append(e)
            lines.append(f'| {did} | {d["family"]} | {d["stage"]} | {fmt(e["fmax"], 1)} | {name} | {fmt(e["rho"])} | '
                         f'{fmt(e["precision"])} | {fmt(e["recall"])} | {fmt(e["fmax_pred"], 1)} | '
                         f'{fmt(100 * e["fmax_err"], 0)} % | {fmt(e["bias_ns"])} |')
    lines += ['', '## Summary (held-out, median over designs; folded and native apart)', '',
              '| designs | model | n | Spearman (median / min) | precision | recall | abs Fmax error (median / max) | within ±15 % |',
              '|---|---|---|---|---|---|---|---|']
    N = np()
    for grp, name in sorted(summ, key=lambda k: (k[0], ['levels', 'v0', 'v1', 'v1-res', 'v1-post'].index(k[1]))):
        E = summ[(grp, name)]
        if not E:
            continue
        g = lambda k: N.array([e[k] for e in E if not math.isnan(e[k])])
        rho, fe = g('rho'), N.abs(g('fmax_err'))
        med = lambda x: fmt(N.median(x)) if len(x) else '—'
        lines.append(f'| {grp} | {name} | {len(E)} | {med(rho)} / {fmt(rho.min()) if len(rho) else "—"} | {med(g("precision"))} | '
                     f'{med(g("recall"))} | {fmt(100 * N.median(fe), 0)} % / {fmt(100 * fe.max(), 0)} % | '
                     f'{int((fe <= 0.15).sum())}/{len(fe)} |')
    if full:
        lines += ['', '## v1 coefficients on all designs [fit]', '', '| term | ns |', '|---|---|']
        lines += [f'| {t} | {c:.4f} |' for t, c in zip(full['terms'], full['coef'])]
        lines += ['', f'Weighted rmse {full["rmse_ns"]:.3f} ns over {len(fitted)} designs.', '']
    lines += [''] + routability(designs)
    lines += design_table(designs)
    txt = '\n'.join(lines) + '\n'
    if a.out:
        open(a.out, 'w').write(txt)
        if a.json and full:
            json.dump(full, open(a.json, 'w'), indent=1)
    print(txt)


def routability(designs, fill=0.80):
    """Design-level route risk before placement: the larger of the unavoidable SLR fill (pre_fill) and the largest
    subtree the floorplan pins whole (blob), both as shares of an SLR's LUTs. Judged (a) by the physical rule,
    risk > `fill` (the fit gate's placement fill), and (b) by a threshold fitted on the other routed/failed builds
    (leave one design out: the midpoint between classes that misclassifies fewest)."""
    imgs = [k for k, d in designs.items() if d['stage'] in ('routed', 'routed_error') and fit_ok(d)]
    risk = {k: max(fnum(designs[k]['pre_fill']), fnum(designs[k]['blob'])) for k in imgs}
    failed = {k: designs[k]['outcome'] == 'failed' for k in imgs}
    out = ['## Routability before placement (design level)', '',
           f'risk = max(pre_fill, blob). Rule: risk > {fill:.2f} (the fit gate\'s fill) flags a route failure; '
           '"LOO" uses a threshold fitted on the other builds [fit].', '',
           '| design | outcome | overlaps | risk | rule | LOO threshold | LOO |', '|---|---|---|---|---|---|---|']
    ok_rule = ok_loo = 0
    for k in sorted(imgs, key=lambda k: risk[k]):
        others = sorted((risk[j], failed[j]) for j in imgs if j != k)
        best = (len(others) + 1, fill)
        cands = sorted(set(r for r, _ in others))
        for i in range(len(cands) + 1):
            t = (cands[i - 1] + cands[i]) / 2 if 0 < i < len(cands) else (cands[0] - 0.01 if i == 0 else cands[-1] + 0.01) if cands else fill
            err = sum((r > t) != f for r, f in others)
            if err < best[0]:
                best = (err, t)
        t = best[1]
        pr, pl = risk[k] > fill, risk[k] > t
        ok_rule += pr == failed[k]
        ok_loo += pl == failed[k]
        d = designs[k]
        out.append(f'| {k} | {d["outcome"]} | {d["overlaps"]} | {risk[k]:.3f} | {"FAIL" if pr else "route"} '
                   f'{"✓" if pr == failed[k] else "✗"} | {t:.3f} | {"FAIL" if pl else "route"} {"✓" if pl == failed[k] else "✗"} |')
    out += ['', f'Correct: rule {ok_rule}/{len(imgs)}, LOO {ok_loo}/{len(imgs)}.', '']
    return out


def design_table(designs):
    out = ['## Designs: locality and routability features', '',
           'pre_fill: the LUT fill no placement can avoid (an SLR\'s floorplan-assigned subtrees, or the design spread '
           'evenly), before placement; blob: the largest subtree pinned whole, as a share of its SLR; placed_fill: '
           'the fullest SLR as placed [M].', '',
           '| design | family | outcome | overlaps | LUTs | regions | pre_fill | blob | placed_fill |', '|---|---|---|---|---|---|---|---|---|']
    for k in sorted(designs, key=lambda k: (designs[k]['family'], k)):
        d = designs[k]
        out.append(f'| {k} | {d["family"]} | {d["outcome"]} | {d["overlaps"]} | {int(d["luts"]):,} | {d["regions"]} | '
                   f'{d["pre_fill"]} | {d["blob"]} | {d["placed_fill"]} |')
    return out + ['']


def cmd_design(a):
    print('\n'.join(design_table(load_designs(a.db))))


def short_inst(cell, depth):
    """The instance a cell belongs to, cut to `depth` levels below the deepest common shell prefix."""
    parts = cell.split('/')[:-1]
    return '/'.join(parts[:depth]) or '(top)'


def cmd_explain(a):
    """The hierarchy edges a model predicts to fail: end points grouped by (start instance, end instance) at a depth,
    with the count, the worst predicted and measured slack, and the term that adds the most delay."""
    N = np()
    designs = load_designs(a.db)
    d = designs[a.design]
    model = json.load(open(a.model)) if a.model else fit_v1(a.db, designs, [k for k in designs if k != a.design and fit_ok(designs[k])])
    rows = [r for r in csv.DictReader(gzip.open(os.path.join(a.db, 'paths', f'{a.design}.tsv.gz'), 'rt'), delimiter='\t')
            if 0 < fnum(r['req']) <= 20]
    P = load_paths(a.db, a.design)
    X = features(P, d)
    coef = N.array(model['coef'])
    contrib = X * coef
    pred = contrib.sum(axis=1)
    ps = P['req'] - pred
    groups = collections.defaultdict(lambda: [0, 0.0, 1e9, 1e9, N.zeros(len(coef)), set()])
    for i, r in enumerate(rows):
        if ps[i] >= a.slack:
            continue
        k = (short_inst(r['s_cell'], a.depth), short_inst(r['e_cell'], a.depth))
        g = groups[k]
        g[0] += 1
        g[1] += fnum(r['weight'], 1)
        g[2] = min(g[2], ps[i])
        g[3] = min(g[3], fnum(r['slack']))
        g[4] += contrib[i]
        g[5].add((r['s_reg'] or 'free') + '>' + (r['e_reg'] or 'free'))
    terms = model['terms']
    print(f'{a.design}: {sum(g[0] for g in groups.values())} end points predicted below {a.slack} ns slack, '
          f'{len(groups)} hierarchy edges (depth {a.depth})')
    for k, g in sorted(groups.items(), key=lambda kv: kv[1][2])[:a.top]:
        avg = g[4] / g[0]
        top = sorted(((avg[j], terms[j]) for j in range(1, len(terms))), reverse=True)[:2]
        print(f'  {g[0]:6d} ep  pred {g[2]:7.2f}  meas {g[3]:7.2f}  {",".join(sorted(g[5]))[:30]:30s}  '
              f'{" + ".join(f"{t} {v:.2f}" for v, t in top)}\n      {k[0]}\n   -> {k[1]}')


def cmd_predict(a):
    """Pre-Vivado mode: a Beacon screen (endpoints.csv, instances.csv) of a design, its floorplan (a flow's floorplan
    Tcl or an export's assign.tsv) and its synthesized hierarchy (report_utilization -hierarchical, or none), scored
    with a fit: the predicted worst delay and Fmax, the end points over the period, and the hierarchy edges behind
    them. Beacon's levels map to Vivado's by --level-scale (1.0 on folds, measured by match; ~0.8 on native RTL)."""
    N = np()
    model = json.load(open(a.model))
    coef = N.array(model['coef'])
    inst = {r['id']: r['instance'] for r in csv.DictReader(open(os.path.join(a.screen, 'instances.csv')))}
    pre = a.prefix.rstrip('/') + '/' if a.prefix else ''
    reg = {}
    for fp in a.floorplan or []:
        reg.update(read_floorplan_tcl(fp) if fp.endswith('.tcl') else load_assign(fp))
    tree, top = parse_util_hier(a.util) if a.util else ({}, None)
    top_luts = top[0] if top else a.luts
    reg, pre_fill, blob, _ = slr_plan(reg, tree, top_luts)
    print(f'floorplan: {len(reg)} assigned cells; unavoidable fill {pre_fill:.2f}, largest pinned subtree {blob:.2f} '
          f'of its SLR')
    rc, lc = {}, {}
    fill = a.fill if a.fill is not None else pre_fill
    d = dict(pre_fill=fill, stage='placed' if a.placed else 'routed')
    rows, feats, allpred = [], [], []
    B = 200000
    worst = []
    over = 0
    edges = collections.Counter()
    def flush():
        nonlocal over
        if not feats:
            return
        F = N.array(feats, dtype=float)
        P = dict(levels=F[:, 0], maxfo=F[:, 1], x_reg=F[:, 2], free=F[:, 3], span=F[:, 4], xdist=F[:, 5], req=N.full(len(F), a.period))
        pred = predict(model, P, d)
        allpred.append(pred)
        for i in N.argsort(-pred)[:a.top]:
            worst.append((float(pred[i]),) + rows[i])
        for i in N.nonzero(pred > a.period)[0]:
            over += 1
            edges[(rows[i][1], rows[i][2])] += 1
        worst.sort(reverse=True)
        del worst[a.top:]
        rows.clear(), feats.clear()
    # Pass 1: the end points' cells; the coarse SLR placement needs every connection first.
    eps_ = []
    with opener(os.path.join(a.screen, 'endpoints.csv')) as f:
        for r in csv.DictReader(f):
            si, ei = inst.get(r['start_inst'], ''), inst.get(r['end_inst'], '')
            s = pre + (si + '/' if si else '') + r['start'].split('[')[0]
            e = pre + (ei + '/' if ei else '') + r['end'].split('[')[0]
            eps_.append((s, e, fnum(r['levels']), fnum(r['fanout'], 1), si, ei, r['start'], r['end']))
    pos = coarse_slr_positions(((s, e, 1.0) for s, e, *_ in eps_), reg)
    for s, e, lv, fo, si, ei, st, en in eps_:
        sr, er = region_of(s, reg, rc), region_of(e, reg, rc)
        L = lca(s, e)
        # the image's hierarchy has the prefix; a synthesis of the screened top alone does not
        luts = subtree_luts(L, tree, top_luts, lc) if tree else top_luts
        if tree and pre and luts == top_luts and L.startswith(pre):
            luts = subtree_luts(L[len(pre):], tree, top_luts, lc)
        xd = abs(pos.get(node_of(s), 1.0) - pos.get(node_of(e), 1.0))
        feats.append((a.level_scale * lv, fo, region_cross(sr, er), int(not sr) + int(not er),
                      math.sqrt(max(luts, 1.0) / 1e3), xd))
        rows.append((str(int(lv)), (si or '(top)'), (ei or '(top)'), sr or 'free', er or 'free', st, en))
        if len(feats) >= B:
            flush()
    flush()
    w = worst[0][0] if worst else 0.0
    allp = N.concatenate(allpred) if allpred else N.zeros(1)
    crit = crit_delay(allp, N.ones(len(allp)))
    print(f'{a.screen}: model {a.model} ({len(model.get("designs", []))} designs); period {a.period} ns, fill {fill:.2f}')
    print(f'predicted critical delay (100th worst end point) {crit:.2f} ns -> {1000 / crit:.1f} MHz; '
          f'worst {w:.2f} ns; {over} end points over {a.period} ns')
    for p_, lv, si, ei, sr, er, st, en in worst:
        print(f'  {p_:6.2f} ns  {lv:>2s} lv  {sr}>{er}  {si[:40]}:{st[:40]} -> {ei[:40]}:{en[:40]}')
    if edges:
        print('end points over the period by instance edge:')
        for (si, ei), k in edges.most_common(12):
            print(f'  {k:8d}  {si} -> {ei}')


UTIL_SAMPLE = """
+----+
| Instance | Module | Total LUTs | Logic LUTs | LUTRAMs | SRLs | FFs | RAMB36 | RAMB18 | URAM | DSP Blocks |
+----+
| top | (top) | 1000 | 900 | 100 | 0 | 50 | 0 | 0 | 0 | 0 |
|   (top) | (top) | 10 | 10 | 0 | 0 | 1 | 0 | 0 | 0 | 0 |
|   a | m | 600 | 600 | 0 | 0 | 20 | 0 | 0 | 0 | 0 |
|     b | m2 | 400 | 400 | 0 | 0 | 10 | 0 | 0 | 0 | 0 |
|   c | m3 | 390 | 390 | 0 | 0 | 10 | 0 | 0 | 0 | 0 |
+----+
"""
FLOORPLAN_SAMPLE = """set c [get_cells -quiet -hierarchical -regexp {.*/g_vg0[./]vmem$}]
if {[llength $c] == 1} { set_property USER_SLR_ASSIGNMENT SLR2 $c }
set c [get_cells -quiet -hierarchical lms_copy]
if {[llength $c] == 1} { set_property USER_SLR_ASSIGNMENT SLR1 $c }
set_property USER_SLR_ASSIGNMENT SLR0 [get_cells top/x]
"""


def cmd_selftest(a):
    """Readers and the fit on small synthetic inputs (no Vivado): run after any change to this file."""
    import tempfile
    N = np()
    with tempfile.TemporaryDirectory(dir=a.dir) as t:
        uh, fp = os.path.join(t, 'u.rpt'), os.path.join(t, 'f.tcl')
        open(uh, 'w').write(UTIL_SAMPLE)
        open(fp, 'w').write(FLOORPLAN_SAMPLE)
        tree, top = parse_util_hier(uh)
        assert top[0] == 1000 and tree['a'][0] == 600 and tree['a/b'][0] == 400 and tree['c'][0] == 390, tree
        reg = read_floorplan_tcl(fp)
    c = {}
    assert region_of('top/sim/g_vg0/vmem/r_reg', reg, c) == 'SLR2', reg
    assert region_of('top/sim/lms_copy/q', reg, c) == 'SLR1'
    assert region_of('top/x/y/z', reg, c) == 'SLR0' and region_of('top/w/z', reg, c) == ''
    assert lca('a/b/r1', 'a/c/r2') == 'a' and lca('a/b/r1', 'a/b/r2') == 'a/b'
    assert region_cross('SLR0', 'SLR2') == 2 and region_cross('SLR1', '') == 0
    # The features are full rank: least squares recovers known coefficients from noiseless synthetic designs.
    rng = N.random.default_rng(1)
    true = N.array([0.5, 0.3, 0.1, 0.2, 0.15, 1.0, 0.5, 0.05, 0.1, -0.2, 0.8, 0.1])
    n = 4000
    P = dict(levels=rng.integers(0, 15, n).astype(float), maxfo=2.0 ** rng.integers(0, 14, n),
             x_reg=rng.integers(0, 3, n).astype(float), free=rng.integers(0, 3, n).astype(float),
             span=rng.uniform(0, 30, n), xdist=rng.uniform(0, 2, n), req=rng.choice([3.3, 5.0, 10.0], n))
    X = N.concatenate([features(P, dict(pre_fill=f, stage=st)) for f, st in ((0.5, 'routed'), (0.8, 'placed'))])
    coef = N.linalg.lstsq(X, X @ true, rcond=None)[0]
    assert N.allclose(coef, true, atol=1e-4), coef
    print('beacon_db selftest: ok')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('ingest')
    p.add_argument('export')
    p.add_argument('--db', required=True)
    p.add_argument('--id')
    p.add_argument('--family', required=True, help='design family: held out together')
    p.add_argument('--kind', default='folded', choices=['folded', 'native'])
    p.add_argument('--stage', required=True, choices=['synth', 'placed', 'routed', 'routed_error'])
    p.add_argument('--outcome', required=True, choices=['routed', 'failed', 'placed'])
    p.add_argument('--overlaps')
    p.add_argument('--floorplan', action='append', help='a floorplan Tcl (USER_SLR_ASSIGNMENT lines) to add')
    p.add_argument('--note')
    p = sub.add_parser('ingest-ep')
    p.add_argument('run')
    p.add_argument('--db', required=True)
    p.add_argument('--id')
    p.add_argument('--family', required=True)
    p.add_argument('--kind', default='folded', choices=['folded', 'native'])
    p = sub.add_parser('list')
    p.add_argument('--db', required=True)
    p = sub.add_parser('fit')
    p.add_argument('--db', required=True)
    p.add_argument('--holdout', nargs='*')
    p.add_argument('--mode', default='res', choices=['res', 'pre', 'post'],
                   help='res: v0 + locality correction (before placement, default); pre: full linear; post: placed')
    p.add_argument('-o', '--out', required=True)
    p = sub.add_parser('report')
    p.add_argument('--db', required=True)
    p.add_argument('-o', '--out')
    p.add_argument('--json', help='also write the fit on every design')
    p.add_argument('--only', nargs='*')
    p.add_argument('--holdout-family', choices=['design', 'family'], default='design',
                   help='hold out the design alone (default) or its whole family')
    p = sub.add_parser('explain')
    p.add_argument('design')
    p.add_argument('--db', required=True)
    p.add_argument('--model', help='a fit JSON (default: fit on every other design)')
    p.add_argument('--depth', type=int, default=8)
    p.add_argument('--slack', type=float, default=0.0)
    p.add_argument('--top', type=int, default=15)
    p = sub.add_parser('predict')
    p.add_argument('screen', help='a Beacon screen directory (endpoints.csv, instances.csv)')
    p.add_argument('--model', required=True)
    p.add_argument('--period', type=float, default=5.0)
    p.add_argument('--prefix', default='', help="the screened top's cell path in the image (e.g. top_i/.../inst/sim)")
    p.add_argument('--floorplan', action='append', help='floorplan Tcl or assign.tsv')
    p.add_argument('--util', help='report_utilization -hierarchical of the synthesized design')
    p.add_argument('--luts', type=float, default=1e6, help='design LUTs when no --util')
    p.add_argument('--fill', type=float)
    p.add_argument('--placed', action='store_true', help='predict the post-place estimate, not the routed delay')
    p.add_argument('--level-scale', type=float, default=1.0,
                   help='Beacon levels to Vivado levels: 1.0 on folds (matched y8b, a32e), ~0.8 on native RTL')
    p.add_argument('--top', type=int, default=15)
    p = sub.add_parser('selftest')
    p.add_argument('--dir', default=os.getcwd(), help='where to put the scratch files (default: here)')
    p = sub.add_parser('ablate')
    p.add_argument('--db', required=True)
    p = sub.add_parser('match')
    p.add_argument('design')
    p.add_argument('screen')
    p.add_argument('--db', required=True)
    p.add_argument('--prefix', default='top_i/slash/foldsim_afu_0/inst/sim')
    p = sub.add_parser('congestion')
    p.add_argument('design')
    p.add_argument('report', help='a place_design log (runme.log, vivado.log) or report_design_analysis -congestion .rpt')
    p.add_argument('--db', required=True)
    p = sub.add_parser('score')
    p.add_argument('design')
    p.add_argument('--db', required=True)
    p.add_argument('--out')
    p = sub.add_parser('design')
    p.add_argument('--db', required=True)
    a = ap.parse_args()
    {'ingest': cmd_ingest, 'ingest-ep': cmd_ingest_ep, 'list': cmd_list, 'fit': cmd_fit, 'report': cmd_report, 'design': cmd_design, 'explain': cmd_explain, 'predict': cmd_predict, 'selftest': cmd_selftest, 'ablate': cmd_ablate, 'match': cmd_match, 'congestion': cmd_congestion, 'score': cmd_score}[a.cmd](a)


if __name__ == '__main__':
    main()
