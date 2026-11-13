"""Invented deterministic data for software checks; never historical market evidence."""
from pathlib import Path
import csv
import json
from datetime import date, datetime, timedelta

ROOT = Path(__file__).resolve().parents[1]

def identified_chain(source):
    """Complete invented contract identity without relaxing quote validation."""
    def fixture():
        chain = source()
        for row in chain.get("optionChain", {}).get("optionContracts", []):
            row["underlying"] = "NIFTY"
        return chain
    fixture.deltas = source.deltas
    return fixture


def generate():
    files = []
    def write(name, value):
        p = ROOT / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value)
        files.append(name)
    def table(name, columns, rows):
        import io
        out = io.StringIO()
        w = csv.writer(out, lineterminator='\n'); w.writerow(columns); w.writerows(rows)
        write(name, out.getvalue())
    write('data/holidays/nse.yaml', '# SYNTHETIC: test closure assumptions only; no exchange authority\nholidays:\n  - {date: 2026-11-10, name: SYNTHETIC_CLOSED_DAY}\n  - {date: 2025-10-22, name: SYNTHETIC_CLOSED_DAY}\n  - {date: 2021-01-26, name: SYNTHETIC_CLOSED_DAY}\n')
    write('data/lot_sizes/nse.yaml', '# SYNTHETIC: test assumptions only\nchanges:\n  - {underlying: NIFTY, from: 2020-01-01, lot_size: 65}\n')
    events = [{'date':str(date(2024,2,8) + timedelta(days=i*13)), 'kind':['rbi_policy','budget','election','fomc'][i%4], 'title':f'SYNTHETIC event {i}'} for i in range(32)]
    write('data/events/synthetic.yaml', '# SYNTHETIC VERIFY: invented dates, not a market calendar\n'+json.dumps({'source':'manual_macro_calendar','events':events},indent=2)+'\n')
    table('data/sample_nifty_options.csv', ['date','close','iv_rank','dte'], [(str(date(2024,1,1)+timedelta(days=i)),20000+(i%9)*13,20+(i*7)%70,7+i%14) for i in range(90)])
    # Simple independently specified option rows exercise filtering and lot fallback.
    legacy=['INSTRUMENT','SYMBOL','EXPIRY_DT','STRIKE_PR','OPTION_TYP','OPEN','HIGH','LOW','CLOSE','SETTLE_PR','CONTRACTS','VAL_INLAKH','OPEN_INT','CHG_IN_OI','TIMESTAMP']
    rows=[]
    for symbol,strike,kind,price in [('NIFTY',21700,'CE',123.25),('NIFTY',21700,'PE',88.5),('NIFTY',22500,'CE',3.25),('BANKNIFTY',45000,'CE',99.5)]:
        rows.append(['OPTIDX',symbol,'11-Jan-2024',strike,kind,price,price+2,price-2,price,price,40000,0,500,0,'05-Jan-2024'])
    rows.append(['FUTIDX','NIFTY','11-Jan-2024',0,'XX',1,2,0,1,1,2,0,1,0,'05-Jan-2024'])
    table('tests/fixtures/nse/fo05JAN2024bhav.csv',legacy,rows)
    udiff=['FinInstrmTp','TckrSymb','XpryDt','TradDt','StrkPric','OptnTp','ClsPric','SttlmPric','TtlTradgVol','OpnIntrst','NewBrdLotQty','UndrlygPric']
    table('tests/fixtures/nse/BhavCopy_NSE_FO_0_0_0_20240708_F_0000.csv',udiff,[['IDO','NIFTY','2024-07-11','2024-07-08',k,t,p,p,123,400,25,24011.5] for k,t,p in [(24300,'CE',77),(24300,'PE',66),(25000,'CE',2)]])
    weekdays=[]; d=date(2024,6,3)
    while d<=date(2024,7,8):
        if d.weekday()<5:weekdays.append(d)
        d+=timedelta(days=1)
    table('tests/fixtures/nse/india_vix_2024.csv',['Date','Close'],[(str(d),round(12+i*.1,2)) for i,d in enumerate(weekdays)])
    table('tests/fixtures/nse/index_NIFTY_2024.csv',['Date','Open','High','Low','Close'], [('2024-07-04',24000,24050,23950,24010),('2024-07-05',24010,24070,23990,24020),('2024-07-08',24020,24080,23980,24011.5)])
    bars=[]
    for day in ['2026-11-03','2026-11-04']:
        for i in range(75):
            start=datetime.fromisoformat(day+'T09:15:00')+timedelta(minutes=i*5)
            price=20000+((i%20)-10)*4+(0 if day.endswith('03') else 30)
            bars.append({'date':start.strftime('%Y-%m-%d %H:%M'),'open':price,'high':price+8,'low':price-8,'close':price+2,'volume':1000+i*10})
    write('tests/fixtures/live_replay/nifty_5m_2026-11-03_04.json',json.dumps({'provenance':'SYNTHETIC invented sawtooth, not market history','bars':bars},indent=2)+'\n')
    # Invented quarantine specimens; excluded research documents are never read.
    catalog = [
        ('liquidity-sweep.md', 'liquidity-sweep', False),
        ('previous-day-breakout.md', 'previous-day-breakout', False),
        ('trendline-breakout.md', 'trendline-breakout', False),
        ('liquidity-sweep-short.extracted.md', 'liquidity-sweep-short', True),
        ('liquidity-sweep-long.extracted.md', 'liquidity-sweep-long', True),
    ]
    for filename, slug, extracted in catalog:
        gap = 'Assumed, not in the source: entry threshold is undefined' if extracted else 'Mandatory entry threshold is undefined'
        write('research/strategies/' + filename,
              '---\nname: ' + slug + '\nprovenance: SYNTHETIC source-only quarantine specimen\n---\n'
              '# Thesis\nInvented software fixture; no market or source-fidelity claim.\n'
              '## Entry\n- Undefined; never execute.\n## Gaps\n- ' + gap + '\n')
    for filename in ('OPENING_RANGE_LIQUIDITY.md', 'CONSOLIDATION_LIQUIDITY.md'):
        write('research/strategies/' + filename,
              '# SYNTHETIC research hypothesis\nInvented non-executable catalog specimen. '
              'No source-fidelity or performance claim; no executable rules.\n')
    write('synthetic-fixture-inventory.json', json.dumps({'provenance':'invented; no original fixtures read','files':files},indent=2)+'\n')

if __name__ == '__main__':
    generate()
