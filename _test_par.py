import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def main():
    from app.config import load_config
    from app.pipeline import train
    from app.data.store import Store
    from app.data import mt5_client
    DB = 'data/_test_par.db'; BLOB = 'data/models/_test_par.joblib'
    cfg = load_config()
    cfg['storage']['path'] = DB
    cfg['_resolved_symbols'] = ['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD']
    cfg['ai']['ml']['min_train_samples'] = 15
    cfg['ai']['ml']['model_path'] = BLOB
    cfg['ai']['ml']['live_outcomes'] = {"enabled": True, "min_samples": 9999}
    store = Store(DB)
    for sym in cfg['_resolved_symbols']:
        for tf in cfg['timeframes']:
            store.upsert_candles(mt5_client.fetch_ohlcv(sym, tf, int(cfg['data']['bars']), demo=True), sym, tf)
    cfg['ai']['ml']['parallel'] = True; cfg['ai']['ml']['workers'] = 3
    t0 = time.time(); m_par = train(cfg, store=store, verbose=False); t_par = time.time()-t0
    print(f"PARALLEL: {m_par['n_samples']} samples in {t_par:.1f}s", flush=True)
    cfg['ai']['ml']['parallel'] = False
    t0 = time.time(); m_ser = train(cfg, store=store, verbose=False); t_ser = time.time()-t0
    print(f"SERIAL:   {m_ser['n_samples']} samples in {t_ser:.1f}s", flush=True)
    print(f"samples match: {m_par['n_samples'] == m_ser['n_samples']} | speedup: {t_ser/t_par:.1f}x", flush=True)
    store.conn.close()
    for f in (DB, BLOB):
        for _ in range(5):
            try: os.remove(f); break
            except PermissionError: time.sleep(1)
    print('CLEANED', flush=True)

if __name__ == '__main__':
    main()
