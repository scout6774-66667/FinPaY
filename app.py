"""
VORTEx FinPay v4 — Real-Time AI Financial Intelligence Platform
═══════════════════════════════════════════════════════════════
✦ Server-Sent Events (SSE) real-time push — no library needed
✦ PostgreSQL PRIMARY (set DATABASE_URL env), SQLite fallback
✦ 14 upgraded features + all 9 original ML modules
Run: python app.py
PostgreSQL: export DATABASE_URL="postgresql://user:pass@host:5432/dbname"
"""

from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from datetime import datetime, timedelta
import math, random, os, statistics, json, queue, threading, time, re
from collections import defaultdict

try:
    import numpy as np
    from sklearn.cluster import KMeans
    from sklearn.ensemble import IsolationForest, RandomForestRegressor, GradientBoostingRegressor
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import Ridge
    from sklearn.metrics.pairwise import cosine_similarity
    from sklearn.preprocessing import StandardScaler
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False

# ── DB setup ────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = bool(DATABASE_URL)
if USE_PG:
    try:
        import psycopg2, psycopg2.extras
        PG_OK = True
    except ImportError:
        USE_PG = False; PG_OK = False
if not USE_PG:
    import sqlite3

app = Flask(__name__)
SQLITE_PATH = os.path.join(os.path.dirname(__file__), "vortex.db")

# ── SSE Registry ────────────────────────────────────────────────────────────
_sse_clients = []
_sse_lock = threading.Lock()

def sse_push(event, data):
    payload = json.dumps({"event": event, "data": data, "ts": now_str()})
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try: q.put_nowait(payload)
            except queue.Full: dead.append(q)
        for q in dead: _sse_clients.remove(q)

@app.route("/api/stream")
def sse_stream():
    q = queue.Queue(maxsize=60)
    with _sse_lock: _sse_clients.append(q)
    def generate():
        yield f"data: {json.dumps({'event':'connected','data':{'ok':True}})}\n\n"
        try:
            while True:
                try:
                    payload = q.get(timeout=28)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            with _sse_lock:
                if q in _sse_clients: _sse_clients.remove(q)
    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no","Connection":"keep-alive"})

# ═══════════════════════════════════════════════════════════════════════════
#  DATABASE
# ═══════════════════════════════════════════════════════════════════════════
def get_db():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def db_ph(): return "%s" if USE_PG else "?"

def init_db():
    conn = get_db(); c = conn.cursor(); p = db_ph()
    if USE_PG:
        c.execute("""
        CREATE TABLE IF NOT EXISTS account(id SERIAL PRIMARY KEY,main_balance REAL DEFAULT 10000,piggy_bank REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS transactions(id SERIAL PRIMARY KEY,date TEXT,type TEXT,amount REAL,category TEXT,receiver TEXT,upi_id TEXT DEFAULT '',is_anomaly INTEGER DEFAULT 0,tx_class TEXT DEFAULT '',notes TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS goals(id SERIAL PRIMARY KEY,name TEXT,target REAL,saved REAL DEFAULT 0,deadline TEXT,created_at TEXT,emoji TEXT DEFAULT '🎯',priority INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS milestones(id SERIAL PRIMARY KEY,desc TEXT,target REAL,progress REAL DEFAULT 0,category TEXT,done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS merchants(id SERIAL PRIMARY KEY,name TEXT,upi TEXT,logo TEXT,cat TEXT);
        CREATE TABLE IF NOT EXISTS budget_limits(id SERIAL PRIMARY KEY,category TEXT UNIQUE,monthly_limit REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS subscriptions_managed(id SERIAL PRIMARY KEY,name TEXT,amount REAL,frequency TEXT,next_date TEXT DEFAULT '',is_active INTEGER DEFAULT 1,usage_score INTEGER DEFAULT 50,notes TEXT DEFAULT '',merchant_upi TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS notifications(id SERIAL PRIMARY KEY,type TEXT,title TEXT,message TEXT,created_at TEXT,is_read INTEGER DEFAULT 0,priority TEXT DEFAULT 'info');
        """)
    else:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY,main_balance REAL DEFAULT 10000,piggy_bank REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS transactions(id INTEGER PRIMARY KEY AUTOINCREMENT,date TEXT,type TEXT,amount REAL,category TEXT,receiver TEXT,upi_id TEXT DEFAULT '',is_anomaly INTEGER DEFAULT 0,tx_class TEXT DEFAULT '',notes TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS goals(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,target REAL,saved REAL DEFAULT 0,deadline TEXT,created_at TEXT,emoji TEXT DEFAULT '🎯',priority INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS milestones(id INTEGER PRIMARY KEY AUTOINCREMENT,desc TEXT,target REAL,progress REAL DEFAULT 0,category TEXT,done INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS merchants(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,upi TEXT,logo TEXT,cat TEXT);
        CREATE TABLE IF NOT EXISTS budget_limits(id INTEGER PRIMARY KEY AUTOINCREMENT,category TEXT UNIQUE,monthly_limit REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS subscriptions_managed(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT,amount REAL,frequency TEXT,next_date TEXT DEFAULT '',is_active INTEGER DEFAULT 1,usage_score INTEGER DEFAULT 50,notes TEXT DEFAULT '',merchant_upi TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,type TEXT,title TEXT,message TEXT,created_at TEXT,is_read INTEGER DEFAULT 0,priority TEXT DEFAULT 'info');
        """)

    c.execute("SELECT id FROM account LIMIT 1")
    if not c.fetchone(): c.execute(f"INSERT INTO account(main_balance,piggy_bank) VALUES({p},{p})",(10000,0))
    c.execute("SELECT id FROM milestones LIMIT 1")
    if not c.fetchone():
        for desc,tgt,cat in [("Spend ₹500 total",500,"Any"),("Spend ₹300 on Food",300,"Food"),
                              ("Spend ₹1000 on Travel",1000,"Travel"),("Spend ₹500 on Shopping",500,"Shopping"),
                              ("Spend ₹200 on Recharge",200,"Recharge"),("Spend ₹2000 total",2000,"Any")]:
            c.execute(f"INSERT INTO milestones(desc,target,category) VALUES({p},{p},{p})",(desc,tgt,cat))
    c.execute("SELECT id FROM merchants LIMIT 1")
    if not c.fetchone():
        for name,upi,logo,cat in [
            ("Amazon","amazon@upi","🛍️","Shopping"),("Swiggy","swiggy@upi","🍔","Food"),
            ("Uber","uber@upi","🚗","Travel"),("Netflix","netflix@upi","🎬","Entertainment"),
            ("Spotify","spotify@upi","🎵","Entertainment"),("Zomato","zomato@upi","🍕","Food"),
            ("BigBasket","bigbasket@upi","🛒","Grocery"),("BookMyShow","bms@upi","🎟️","Entertainment"),
            ("Ola","ola@upi","🚕","Travel"),("Jio","jio@upi","📱","Recharge"),
            ("PhonePe","phonepe@upi","💜","Transfer"),("Paytm","paytm@upi","💙","Transfer"),
            ("Dunzo","dunzo@upi","📦","Grocery"),("MakeMyTrip","mmt@upi","✈️","Travel"),
        ]:
            c.execute(f"INSERT INTO merchants(name,upi,logo,cat) VALUES({p},{p},{p},{p})",(name,upi,logo,cat))
    c.execute("SELECT id FROM budget_limits LIMIT 1")
    if not c.fetchone():
        for cat,lim in [("Food",5000),("Travel",3000),("Shopping",4000),("Entertainment",2000),("Grocery",3000),("Recharge",500),("Health",2000)]:
            if USE_PG:
                c.execute(f"INSERT INTO budget_limits(category,monthly_limit) VALUES({p},{p}) ON CONFLICT(category) DO NOTHING",(cat,lim))
            else:
                c.execute(f"INSERT OR IGNORE INTO budget_limits(category,monthly_limit) VALUES({p},{p})",(cat,lim))
    conn.commit(); conn.close()

def now_str(): return datetime.now().strftime("%Y-%m-%d %H:%M")

def get_account():
    conn=get_db(); c=conn.cursor(); c.execute("SELECT * FROM account ORDER BY id LIMIT 1"); r=c.fetchone(); conn.close()
    return dict(r) if r else {"main_balance":10000,"piggy_bank":0,"id":1}

def update_account(main=None, piggy=None):
    conn=get_db(); c=conn.cursor(); p=db_ph()
    if main  is not None: c.execute(f"UPDATE account SET main_balance={p} WHERE id=1",(main,))
    if piggy is not None: c.execute(f"UPDATE account SET piggy_bank={p}   WHERE id=1",(piggy,))
    conn.commit(); conn.close()

def add_transaction(type_,amount,category,receiver,upi_id="",is_anomaly=0,tx_class="",notes=""):
    conn=get_db(); c=conn.cursor(); p=db_ph()
    c.execute(f"INSERT INTO transactions(date,type,amount,category,receiver,upi_id,is_anomaly,tx_class,notes) VALUES({p},{p},{p},{p},{p},{p},{p},{p},{p})",
              (now_str(),type_,amount,category,receiver,upi_id,is_anomaly,tx_class,notes))
    conn.commit(); conn.close()

def get_transactions(limit=500):
    conn=get_db(); c=conn.cursor(); p=db_ph()
    c.execute(f"SELECT * FROM transactions ORDER BY id DESC LIMIT {p}",(limit,))
    rows=c.fetchall(); conn.close(); return [dict(r) for r in rows]

def add_notification(type_,title,message,priority="info"):
    conn=get_db(); c=conn.cursor(); p=db_ph()
    c.execute(f"INSERT INTO notifications(type,title,message,created_at,priority) VALUES({p},{p},{p},{p},{p})",
              (type_,title,message,now_str(),priority))
    conn.commit(); conn.close()
    sse_push("notification",{"type":type_,"title":title,"message":message,"priority":priority})

# ═══════════════════════════════════════════════════════════════════════════
#  ML: WEIGHTED SEQUENCE PREDICTION
# ═══════════════════════════════════════════════════════════════════════════
def weighted_sequence_predict(values, alpha=0.3):
    n=len(values)
    if n==0: return 0.0,0,"stable"
    if n==1: return round(values[0],2),40,"stable"
    if not ML_AVAILABLE: return round(sum(values)/n,2),35,"stable"
    weights=np.array([(1-alpha)**(n-1-i) for i in range(n)]); weights/=weights.sum()
    wm=float(np.dot(weights,values))
    if n>=4:
        smooth=np.convolve(values,[0.33,0.33,0.34],mode='same')
        X=np.column_stack([np.arange(n),np.arange(n)**2,smooth]); scaler=StandardScaler()
        Xs=scaler.fit_transform(X); model=Ridge(alpha=1.0); model.fit(Xs,np.array(values))
        nxt=scaler.transform([[n,n**2,values[-1]]]); ridge=float(model.predict(nxt)[0])
        pred=max(0.0,0.6*wm+0.4*ridge)
    else: pred=wm
    if n>=3:
        cv=statistics.stdev(values)/(statistics.mean(values)+1); conf=min(95,max(30,int(85-cv*40)))
        ra=sum(values[-3:])/3; oa=sum(values[:-3])/max(1,n-3) if n>3 else values[0]
        trend="rising" if ra>oa*1.05 else ("falling" if ra<oa*0.95 else "stable")
    else: conf,trend=35,"stable"
    return round(pred,2),conf,trend

def predict_by_category_weighted(transactions):
    spend_txs=[t for t in transactions if t['type']=='Spend']; results={}
    for cat in {t['category'] for t in spend_txs}:
        vals=[t['amount'] for t in spend_txs if t['category']==cat]
        if not vals: continue
        pred,conf,trend=weighted_sequence_predict(vals)
        results[cat]={"prediction":pred,"confidence":conf,"trend":trend,"avg":round(sum(vals)/len(vals),2),"count":len(vals)}
    return results

# ═══════════════════════════════════════════════════════════════════════════
#  ML: RF SPENDING PREDICTION (GradientBoosting + features)
# ═══════════════════════════════════════════════════════════════════════════
def rf_spending_prediction(transactions):
    if not ML_AVAILABLE: return {}
    spend_txs=[t for t in transactions if t['type']=='Spend']
    if len(spend_txs)<8: return {}
    monthly=defaultdict(float)
    for t in spend_txs: monthly[t['date'][:7]]+=t['amount']
    vals=[v for _,v in sorted(monthly.items())]
    if len(vals)<4: return {}
    features,targets=[],[]
    for i in range(2,len(vals)):
        r3=sum(vals[max(0,i-3):i])/min(3,i); std=statistics.stdev(vals[max(0,i-2):i]) if i>=2 else 0
        features.append([i,r3,std,vals[i-1]]); targets.append(vals[i])
    if len(features)<3: return {}
    X=np.array(features); y=np.array(targets); scaler=StandardScaler(); Xs=scaler.fit_transform(X)
    model=GradientBoostingRegressor(n_estimators=50,max_depth=3,random_state=42); model.fit(Xs,y)
    n=len(vals); r3=sum(vals[-3:])/3; std=statistics.stdev(vals[-2:]) if len(vals)>=2 else 0
    nf=scaler.transform([[n,r3,std,vals[-1]]]); next_pred=max(0,float(model.predict(nf)[0]))
    return {"next_month_total":round(next_pred,2),"by_category":predict_by_category_weighted(transactions),
            "model":"GradientBoosting+WExp","months_analyzed":len(vals),"current_monthly_avg":round(sum(vals)/len(vals),2)}

# ═══════════════════════════════════════════════════════════════════════════
#  ML: FRAUD DETECTION
# ═══════════════════════════════════════════════════════════════════════════
def detect_anomalies(transactions):
    spend_txs=[t for t in transactions if t['type']=='Spend']
    if len(spend_txs)<5: return []
    amounts=[t['amount'] for t in spend_txs]
    q1,q3=np.percentile(amounts,25),np.percentile(amounts,75); iqr=q3-q1; iqr_hi=q3+2.5*iqr
    mean_a,std_a=np.mean(amounts),np.std(amounts)+1e-9; iso_labels={}
    if ML_AVAILABLE and len(amounts)>=8:
        cats=list({t['category'] for t in spend_txs})
        X=np.array([[t['amount'],cats.index(t['category'])] for t in spend_txs])
        iso=IsolationForest(contamination=0.1,random_state=42); iso.fit(X)
        for i,tx in enumerate(spend_txs): iso_labels[tx['id']]=(iso.predict([X[i]])[0]==-1)
    anomalies=[]
    for tx in spend_txs:
        a,votes,reasons=tx['amount'],0,[]
        z=abs((a-mean_a)/std_a)
        if a>iqr_hi: votes+=1; reasons.append("unusually large")
        if z>2.5: votes+=1; reasons.append(f"z={z:.1f}σ")
        if tx.get('id') in iso_labels and iso_labels[tx['id']]: votes+=1; reasons.append("IsolationForest")
        if votes>=2: anomalies.append({"id":tx['id'],"date":tx['date'],"receiver":tx['receiver'],
                                        "amount":a,"category":tx['category'],"anomaly_score":min(99,int(z*20+votes*15)),"reasons":reasons,"votes":votes})
    return sorted(anomalies,key=lambda x:-x['anomaly_score'])

def flag_new_transaction(amount,category,all_transactions):
    spend_txs=[t for t in all_transactions if t['type']=='Spend']
    if len(spend_txs)<5: return False,0
    cat_amounts=[t['amount'] for t in spend_txs if t['category']==category] or [t['amount'] for t in spend_txs]
    mean_a=sum(cat_amounts)/len(cat_amounts); std_a=statistics.stdev(cat_amounts) if len(cat_amounts)>1 else mean_a*0.3
    z=abs((amount-mean_a)/(std_a+1e-9))
    sorted_a=sorted(cat_amounts); q3=sorted_a[int(len(sorted_a)*0.75)]; q1=sorted_a[int(len(sorted_a)*0.25)]
    iqr_hi=q3+2.5*(q3-q1+1); is_anom=(z>2.5 and amount>iqr_hi)
    return is_anom,min(99,int(z*20)) if is_anom else 0

# ═══════════════════════════════════════════════════════════════════════════
#  ML: NLP CATEGORY CLASSIFIER
# ═══════════════════════════════════════════════════════════════════════════
CATEGORY_CORPUS={
    "Food":["swiggy zomato dominos kfc mcdonalds pizza burger biryani restaurant cafe lunch dinner breakfast meal"],
    "Travel":["uber ola rapido bus train metro taxi cab flight petrol fuel commute ride auto rickshaw"],
    "Shopping":["amazon flipkart myntra meesho ajio mall clothes fashion shoes electronics gadget shop"],
    "Entertainment":["netflix hotstar prime spotify youtube movies cinema theatre show concert gaming bookmyshow"],
    "Grocery":["bigbasket grofers dmart reliance fresh vegetables fruits milk grocery blinkit zepto"],
    "Recharge":["jio airtel bsnl vi vodafone idea recharge mobile plan internet data"],
    "Health":["hospital pharmacy medicine doctor clinic apollo fortis health insurance chemist medplus"],
    "Charity":["donate donation ngo fund relief charity pm care"],
    "Transfer":["transfer sent friend family personal phonepe paytm neft rtgs"],
    "Others":["other misc payment bill fee charge"],
}
_tfidf=None; _tlabels=None; _tdocs=None

def _build_tfidf():
    global _tfidf,_tlabels,_tdocs
    if _tfidf: return
    _tlabels=list(CATEGORY_CORPUS.keys()); _tdocs=[v[0] for v in CATEGORY_CORPUS.values()]
    _tfidf=TfidfVectorizer(ngram_range=(1,2)); _tfidf.fit(_tdocs)

def classify_transaction(receiver_name,amount=0):
    if not ML_AVAILABLE: return "Others"
    rl=receiver_name.lower()
    kw={"swiggy":"Food","zomato":"Food","dominos":"Food","kfc":"Food","mcdonalds":"Food","pizza":"Food",
        "uber":"Travel","ola":"Travel","rapido":"Travel","metro":"Travel",
        "amazon":"Shopping","flipkart":"Shopping","myntra":"Shopping","meesho":"Shopping",
        "netflix":"Entertainment","spotify":"Entertainment","hotstar":"Entertainment","bookmyshow":"Entertainment",
        "bigbasket":"Grocery","dmart":"Grocery","grofers":"Grocery","blinkit":"Grocery","zepto":"Grocery",
        "jio":"Recharge","airtel":"Recharge","bsnl":"Recharge",
        "hospital":"Health","pharmacy":"Health","chemist":"Health","apollo":"Health","medplus":"Health",
        "phonepe":"Transfer","paytm":"Transfer"}
    for k,cat in kw.items():
        if k in rl: return cat
    try:
        _build_tfidf(); mat=_tfidf.transform(_tdocs+[rl]); sims=cosine_similarity(mat[-1],mat[:-1])[0]
        best=int(np.argmax(sims)); return _tlabels[best] if sims[best]>0.05 else "Others"
    except: return "Others"

# ═══════════════════════════════════════════════════════════════════════════
#  ML: MERCHANT RECOMMENDATIONS (KNN-style)
# ═══════════════════════════════════════════════════════════════════════════
def merchant_recommendations(transactions):
    spend_txs=[t for t in transactions if t['type']=='Spend']
    if len(spend_txs)<3: return {"recommendations":[],"reason":"Need more transactions"}
    cat_spend=defaultdict(float)
    for t in spend_txs: cat_spend[t['category']]+=t['amount']
    top_cats=sorted(cat_spend,key=cat_spend.get,reverse=True)[:3]
    CAT_M={"Food":[("Swiggy","swiggy@upi"),("Zomato","zomato@upi"),("Dominos","dominos@upi")],
            "Travel":[("Uber","uber@upi"),("Ola","ola@upi"),("MakeMyTrip","mmt@upi")],
            "Shopping":[("Amazon","amazon@upi"),("Flipkart","flipkart@upi"),("Myntra","myntra@upi")],
            "Entertainment":[("Netflix","netflix@upi"),("Spotify","spotify@upi"),("BookMyShow","bms@upi")],
            "Grocery":[("BigBasket","bigbasket@upi"),("Blinkit","blinkit@upi"),("Zepto","zepto@upi")],
            "Recharge":[("Jio","jio@upi"),("Airtel","airtel@upi")],
            "Health":[("Apollo","apollo@upi"),("MedPlus","medplus@upi")]}
    used={t['receiver'].lower() for t in spend_txs}; recs=[]
    for cat in top_cats:
        if cat not in CAT_M: continue
        for name,upi in CAT_M[cat]:
            if name.lower() not in used:
                recs.append({"name":name,"upi":upi,"category":cat,"reason":f"Popular in {cat} (₹{cat_spend[cat]:.0f})"})
        if len(recs)>=6: break
    return {"recommendations":recs[:6],"based_on_categories":top_cats}

# ═══════════════════════════════════════════════════════════════════════════
#  ML: HEALTH SCORE
# ═══════════════════════════════════════════════════════════════════════════
def compute_health_score(account,transactions,goals):
    spend_txs=[t for t in transactions if t['type']=='Spend']
    deposit_txs=[t for t in transactions if t['type']=='Deposit']
    save_txs=[t for t in transactions if t['type'] in ('Round-Up','GoalSave','Cashback')]
    total_in=sum(t['amount'] for t in deposit_txs) or 1
    total_spend=sum(t['amount'] for t in spend_txs)
    total_saved=sum(t['amount'] for t in save_txs)+account['piggy_bank']
    balance=account['main_balance']; scores={}
    sr=total_saved/total_in
    scores['savings_rate']={"score":min(25,round(sr*125)),"max":25,"value":f"{sr*100:.1f}%","label":"Savings Rate","tip":"Aim 20%+" if sr<0.2 else "Great!"}
    if len(spend_txs)>=3:
        amounts=[t['amount'] for t in spend_txs]; cv=statistics.stdev(amounts)/(statistics.mean(amounts)+1)
        stab=max(0,1-cv*0.5); ss=round(stab*20)
    else: stab,ss=0.5,10
    scores['spend_stability']={"score":ss,"max":20,"value":f"{stab*100:.0f}%","label":"Spend Stability","tip":"Irregular" if ss<12 else "Consistent!"}
    cat_spend=defaultdict(float)
    for t in spend_txs: cat_spend[t['category']]+=t['amount']
    max_ratio=max((v/total_spend for v in cat_spend.values()),default=0) if total_spend>0 else 0
    adherence=max(0,1-max(0,max_ratio-0.35)*3); adh=round(adherence*20)
    top_cat=max(cat_spend,key=cat_spend.get) if cat_spend else "None"
    scores['budget_adherence']={"score":adh,"max":20,"value":f"{adherence*100:.0f}%","label":"Budget Adherence","tip":f"'{top_cat}' dominates" if adh<14 else "Well balanced!"}
    if goals:
        avg_prog=sum(min(g['saved']/g['target'],1) for g in goals if g['target']>0)/len(goals); gs=round(avg_prog*20)
        tip=f"{len(goals)} goals active" if avg_prog<0.5 else "Excellent!"
    else: avg_prog,gs,tip=0.25,5,"Create goals"
    scores['goal_progress']={"score":gs,"max":20,"value":f"{avg_prog*100:.0f}%","label":"Goal Progress","tip":tip}
    monthly_spend=total_spend/max(1,len({t['date'][:7] for t in spend_txs})) if spend_txs else 1
    buf=balance/(monthly_spend+1); bs=min(15,round(buf/3*15))
    scores['emergency_buffer']={"score":bs,"max":15,"value":f"{buf:.1f}mo","label":"Emergency Buffer","tip":f"Build {max(0,3-buf):.1f}mo more" if buf<3 else "Healthy!"}
    total=sum(v['score'] for v in scores.values())
    grade="A+" if total>=90 else "A" if total>=80 else "B+" if total>=70 else "B" if total>=60 else "C+" if total>=50 else "C" if total>=40 else "D"
    status="Excellent" if total>=80 else "Good" if total>=60 else "Fair" if total>=40 else "Needs Work"
    return {"total_score":total,"grade":grade,"status":status,"pillars":scores,"top_tip":min(scores.values(),key=lambda x:x['score'])['tip']}

# ═══════════════════════════════════════════════════════════════════════════
#  ML: FINANCIAL BEHAVIOR AI SCORE
# ═══════════════════════════════════════════════════════════════════════════
def compute_behavior_score(account,transactions,goals):
    spend_txs=[t for t in transactions if t['type']=='Spend']
    deposit_txs=[t for t in transactions if t['type']=='Deposit']
    save_txs=[t for t in transactions if t['type'] in ('Round-Up','GoalSave')]
    total_in=sum(t['amount'] for t in deposit_txs) or 1
    total_spend=sum(t['amount'] for t in spend_txs)
    metrics={}
    sr=sum(t['amount'] for t in save_txs)/total_in
    metrics['savings_ratio']={"score":min(20,round(sr*100)),"label":"Savings Ratio","value":f"{sr*100:.1f}%"}
    if len(spend_txs)>=3:
        amounts=[t['amount'] for t in spend_txs]; cv=statistics.stdev(amounts)/(statistics.mean(amounts)+1)
        ss=round(max(0,1-cv*0.4)*20)
    else: ss=10
    metrics['expense_stability']={"score":ss,"label":"Expense Stability","value":f"{ss*5}%"}
    anom_count=sum(1 for t in transactions if t.get('is_anomaly',0)==1)
    metrics['fraud_risk']={"score":max(0,20-anom_count*4),"label":"Fraud Risk","value":f"{anom_count} flagged"}
    cat_spend=defaultdict(float)
    for t in spend_txs: cat_spend[t['category']]+=t['amount']
    over_budget=sum(1 for v in cat_spend.values() if v>5000)
    metrics['budget_discipline']={"score":max(0,20-over_budget*5),"label":"Budget Discipline","value":f"{over_budget} over"}
    if goals:
        avg_prog=sum(min(g['saved']/g['target'],1) for g in goals if g['target']>0)/len(goals)
        metrics['goal_completion']={"score":round(avg_prog*10),"label":"Goal Completion","value":f"{avg_prog*100:.0f}%"}
    else: metrics['goal_completion']={"score":3,"label":"Goal Completion","value":"0%"}
    months=len({t['date'][:7] for t in transactions})
    metrics['payment_regularity']={"score":min(10,months*2),"label":"Payment Regularity","value":f"{months} months"}
    total=sum(v['score'] for v in metrics.values())
    grade="Excellent" if total>=80 else "Good" if total>=60 else "Fair" if total>=40 else "Needs Work"
    return {"total_score":total,"max_score":100,"grade":grade,"metrics":metrics,"insight":f"Discipline score: {total}/100 ({grade})"}

# ═══════════════════════════════════════════════════════════════════════════
#  ML: CREDIT SCORE SIMULATOR
# ═══════════════════════════════════════════════════════════════════════════
def simulate_credit_score(account,transactions,goals):
    spend_txs=[t for t in transactions if t['type']=='Spend']
    deposit_txs=[t for t in transactions if t['type']=='Deposit']
    save_txs=[t for t in transactions if t['type'] in ('Round-Up','GoalSave')]
    total_in=sum(t['amount'] for t in deposit_txs) or 1
    total_spend=sum(t['amount'] for t in spend_txs)
    balance=account['main_balance']; anom_count=sum(1 for t in transactions if t.get('is_anomaly',0)==1)
    on_time=max(0,1-anom_count*0.1); payment_pts=round(on_time*210)
    util=min(1,total_spend/max(total_in,1)); util_pts=round((1-util*0.7)*180)
    months=len({t['date'][:7] for t in transactions}); history_pts=min(90,months*9)
    cats=len({t['category'] for t in spend_txs}); mix_pts=min(60,cats*8)
    sr=sum(t['amount'] for t in save_txs)/total_in; health_pts=min(60,round(sr*120))
    score=min(900,max(300,300+payment_pts+util_pts+history_pts+mix_pts+health_pts))
    rating=("Excellent (760-900)" if score>=760 else "Very Good (700-759)" if score>=700 else
            "Good (650-699)" if score>=650 else "Fair (600-649)" if score>=600 else "Poor (<600)")
    tips=[]
    if anom_count>0: tips.append("Reduce unusual transactions")
    if util>0.7: tips.append("Reduce spending-to-income ratio")
    if months<6: tips.append("Build longer transaction history")
    if sr<0.1: tips.append("Save at least 10% of income")
    return {"score":score,"rating":rating,"tips":tips,
            "breakdown":{"payment":payment_pts,"utilisation":util_pts,"history":history_pts,"mix":mix_pts,"health":health_pts}}

# ═══════════════════════════════════════════════════════════════════════════
#  ML: AI GOAL OPTIMIZER
# ═══════════════════════════════════════════════════════════════════════════
def optimize_goal(goal,transactions,monthly_income=0):
    remaining=max(0,goal['target']-goal['saved'])
    if remaining==0: return {"status":"completed","monthly_suggested":0,"months_needed":0,"feasibility":"Completed"}
    save_txs=[t for t in transactions if t['type'] in ('Round-Up','GoalSave','Cashback')]
    monthly_saves=defaultdict(float)
    for t in save_txs: monthly_saves[t['date'][:7]]+=t['amount']
    avg_save=sum(monthly_saves.values())/len(monthly_saves) if monthly_saves else 500
    spend_txs=[t for t in transactions if t['type']=='Spend']
    monthly_spend=defaultdict(float)
    for t in spend_txs: monthly_spend[t['date'][:7]]+=t['amount']
    avg_spend=sum(monthly_spend.values())/len(monthly_spend) if monthly_spend else 20000
    if not monthly_income:
        dep_txs=[t for t in transactions if t['type']=='Deposit']
        dep_monthly=defaultdict(float)
        for t in dep_txs: dep_monthly[t['date'][:7]]+=t['amount']
        monthly_income=sum(dep_monthly.values())/len(dep_monthly) if dep_monthly else 50000
    available=max(0,monthly_income-avg_spend)
    if goal.get('deadline'):
        try:
            dl=datetime.strptime(goal['deadline'],"%Y-%m-%d"); ml=max(1,(dl-datetime.now()).days//30)
            required=round(remaining/ml,2)
        except: ml,required=18,round(remaining/18,2)
    else: ml=max(1,math.ceil(remaining/max(avg_save,100))); required=round(remaining/ml,2)
    fpct=(available/required*100) if required>0 else 100
    feasibility=("Very Feasible" if fpct>=150 else "Feasible" if fpct>=100 else "Tight" if fpct>=70 else "Challenging")
    suggested=round(required*1.10,2); revised=math.ceil(remaining/suggested) if suggested>0 else ml
    roundup=round(avg_save*0.6,2); manual=max(0,round(suggested-roundup,2))
    scenarios=[{"name":"Conservative","monthly":round(required*0.8,2),"months":math.ceil(remaining/(required*0.8)) if required>0 else 99},
               {"name":"Recommended (AI)","monthly":suggested,"months":revised},
               {"name":"Aggressive","monthly":round(required*1.5,2),"months":math.ceil(remaining/(required*1.5)) if required>0 else 1}]
    return {"status":"active","remaining":round(remaining,2),"monthly_suggested":suggested,
            "months_needed":revised,"feasibility":feasibility,"feasibility_pct":round(fpct,1),
            "available_monthly":round(available,2),"roundup_contribution":roundup,"manual_needed":manual,
            "scenarios":scenarios,"ai_tip":f"Save ₹{suggested}/month → reach goal in {revised} months. Round-ups contribute ~₹{roundup}/month."}

# ═══════════════════════════════════════════════════════════════════════════
#  ML: LIFE DECISION SIMULATOR
# ═══════════════════════════════════════════════════════════════════════════
def life_decision_simulate(decision_type,params,account,transactions,goals):
    spend_txs=[t for t in transactions if t['type']=='Spend']
    dep_txs=[t for t in transactions if t['type']=='Deposit']
    mspend_d=defaultdict(float); mincome_d=defaultdict(float)
    for t in spend_txs: mspend_d[t['date'][:7]]+=t['amount']
    for t in dep_txs: mincome_d[t['date'][:7]]+=t['amount']
    avg_spend=sum(mspend_d.values())/len(mspend_d) if mspend_d else 20000
    monthly_income=sum(mincome_d.values())/len(mincome_d) if mincome_d else 50000
    current_hs=compute_health_score(account,transactions,goals)
    current_score=current_hs['total_score']
    result={"decision_type":decision_type,"current_health":current_score}
    if decision_type=="car":
        price=float(params.get('price',600000)); down=float(params.get('down_payment',100000))
        years=float(params.get('tenure_years',5)); rate=float(params.get('interest_rate',9.5))/100
        principal=price-down; n=int(years*12); r=rate/12
        emi=principal*r*(1+r)**n/((1+r)**n-1) if r>0 else principal/n
        drop_pct=round((emi/monthly_income)*100,1); new_health=max(0,current_score-round(drop_pct*0.8))
        gd=math.ceil(emi*6/max(1,monthly_income-avg_spend-emi)) if (monthly_income-avg_spend-emi)>0 else 99
        result.update({"emi":round(emi,2),"savings_impact_pct":-drop_pct,"health_after":new_health,
                        "health_change":new_health-current_score,"goal_delay_months":gd,
                        "recommendation":"PROCEED" if drop_pct<30 else ("CAUTION" if drop_pct<45 else "NOT RECOMMENDED"),
                        "summary":f"EMI ₹{emi:,.0f}/month. Savings drop {drop_pct}%. Health score {current_score}→{new_health}."})
    elif decision_type=="job_change":
        new_income=float(params.get('new_income',monthly_income)); chg_pct=round((new_income-monthly_income)/monthly_income*100,1)
        new_health=min(100,current_score+round(chg_pct*0.3))
        result.update({"new_monthly_income":round(new_income,2),"income_change_pct":chg_pct,
                        "new_monthly_savings":round(max(0,new_income-avg_spend),2),"health_after":new_health,
                        "health_change":new_health-current_score,"summary":f"Income changes {chg_pct:+.1f}%. Health {current_score}→{new_health}."})
    elif decision_type=="house_rent":
        new_rent=float(params.get('new_rent',15000)); cur_rent=float(params.get('current_rent',10000))
        inc=new_rent-cur_rent; ipct=round(inc/monthly_income*100,1); new_health=max(0,current_score-round(ipct*0.6))
        result.update({"rent_increase":round(inc,2),"income_impact_pct":ipct,"health_after":new_health,
                        "health_change":new_health-current_score,"summary":f"Rent up ₹{inc:,.0f}/mo. {ipct}% of income."})
    return result

# ═══════════════════════════════════════════════════════════════════════════
#  ML: SMART BUDGET ALLOCATOR
# ═══════════════════════════════════════════════════════════════════════════
def smart_budget_allocate(income,transactions):
    spend_txs=[t for t in transactions if t['type']=='Spend']
    cat_history=defaultdict(list)
    for t in spend_txs: cat_history[t['category']].append(t['amount'])
    CAT_META={"Food":{"bucket":"needs","emoji":"🍔"},"Grocery":{"bucket":"needs","emoji":"🛒"},
               "Recharge":{"bucket":"needs","emoji":"📱"},"Health":{"bucket":"needs","emoji":"💊"},
               "Travel":{"bucket":"wants","emoji":"🚗"},"Entertainment":{"bucket":"wants","emoji":"🎬"},
               "Shopping":{"bucket":"wants","emoji":"🛍️"},"Charity":{"bucket":"savings","emoji":"❤️"},
               "Others":{"bucket":"wants","emoji":"💳"}}
    BUCKET_LIMITS={"needs":0.50,"wants":0.30,"savings":0.20}
    total_hist=sum(sum(v) for v in cat_history.values()) or 1
    allocations={}
    for cat,meta in CAT_META.items():
        hist=cat_history.get(cat,[])
        pred,conf,trend=weighted_sequence_predict(hist) if hist else (0,0,"stable")
        hist_ratio=sum(hist)/total_hist if hist else 0
        bcats=[k for k,v in CAT_META.items() if v["bucket"]==meta["bucket"]]
        baseline=income*BUCKET_LIMITS[meta["bucket"]]/len(bcats); historical=hist_ratio*income*0.8
        blended=round(0.4*baseline+0.6*historical,2)
        if trend=="rising": blended=round(blended*1.10,2)
        allocations[cat]={"allocated":max(blended,100),"predicted_spend":pred,"trend":trend,
                           "confidence":conf,"bucket":meta["bucket"],"emoji":meta["emoji"],
                           "surplus":round(max(blended,100)-pred,2)}
    for bucket in ["needs","wants","savings"]:
        bcats=[c for c,v in allocations.items() if v["bucket"]==bucket]
        btotal=sum(allocations[c]["allocated"] for c in bcats); limit=income*BUCKET_LIMITS[bucket]
        if btotal>limit:
            scale=limit/btotal
            for c in bcats:
                allocations[c]["allocated"]=round(allocations[c]["allocated"]*scale,2)
                allocations[c]["surplus"]=round(allocations[c]["allocated"]-allocations[c]["predicted_spend"],2)
    return allocations

# ═══════════════════════════════════════════════════════════════════════════
#  ML: SUBSCRIPTION DETECTOR (enhanced)
# ═══════════════════════════════════════════════════════════════════════════
def detect_subscriptions(transactions):
    spend_txs=sorted([t for t in transactions if t['type']=='Spend'],key=lambda x:x['date'])
    merchant_dates=defaultdict(list)
    for t in spend_txs:
        key=(t['receiver'].lower().strip(),round(t['amount']/50)*50)
        merchant_dates[key].append((t['date'],t['amount']))
    subs=[]
    for (merchant,_),occurrences in merchant_dates.items():
        if len(occurrences)>=2:
            try:
                dates=[datetime.strptime(d,"%Y-%m-%d %H:%M") for d,_ in occurrences]
                gaps=[(dates[i+1]-dates[i]).days for i in range(len(dates)-1)]
                avg_gap=sum(gaps)/len(gaps); avg_amt=sum(a for _,a in occurrences)/len(occurrences)
                freq="Monthly" if 7<=avg_gap<=35 else ("Weekly" if 1<=avg_gap<=8 else "Recurring")
                usage=min(100,len(occurrences)*15)
                annual=avg_amt*(12 if freq=="Monthly" else 52 if freq=="Weekly" else 6)
                subs.append({"name":merchant.title(),"amount":round(avg_amt,2),"frequency":freq,
                              "occurrences":len(occurrences),"last_date":occurrences[-1][0],
                              "usage_score":usage,"annual_cost":round(annual,2),
                              "cancel_savings":round(annual,2),
                              "recommendation":"Keep" if usage>60 else ("Review" if usage>30 else "Consider cancelling")})
            except: pass
    return subs

# ═══════════════════════════════════════════════════════════════════════════
#  ML: MONTE CARLO
# ═══════════════════════════════════════════════════════════════════════════
def monte_carlo_simulate(monthly_income,monthly_expense,months=12,n_simulations=500):
    if not ML_AVAILABLE: return {"p25":0,"p50":0,"p75":0,"median_savings":0,"risk_of_deficit":0,"paths":[]}
    rng=np.random.default_rng(42); all_terminal=[]; sample_paths=[]
    for sim in range(n_simulations):
        path=[]; cum=0
        for m in range(months):
            iv=rng.uniform(0.90,1.10); ev=rng.uniform(0.93,1.12); net=monthly_income*iv-monthly_expense*ev
            cum+=net; path.append(round(cum,2))
        all_terminal.append(cum)
        if sim<20: sample_paths.append(path)
    ts=np.array(all_terminal)
    return {"p25":round(float(np.percentile(ts,25)),2),"p50":round(float(np.percentile(ts,50)),2),
            "p75":round(float(np.percentile(ts,75)),2),"median_savings":round(float(np.median(ts)),2),
            "risk_of_deficit":round(float(np.mean(ts<0)*100),1),
            "min":round(float(np.min(ts)),2),"max":round(float(np.max(ts)),2),
            "sample_paths":sample_paths[:10],"months":months,"n_simulations":n_simulations}

# ═══════════════════════════════════════════════════════════════════════════
#  ML: GOAL FORECAST
# ═══════════════════════════════════════════════════════════════════════════
def goal_achievement_forecast(goal,transactions,n_simulations=300):
    if not ML_AVAILABLE: return {"probability":50,"months_p50":12,"status":"unknown","pct_complete":0}
    save_txs=[t for t in transactions if t['type'] in ('GoalSave','Round-Up','Cashback')]
    ms=defaultdict(float)
    for t in save_txs: ms[t['date'][:7]]+=t['amount']
    sv=[v for _,v in sorted(ms.items())]
    if not sv: sv=[500.0]
    avg_save,conf,trend=weighted_sequence_predict(sv); avg_save=max(avg_save,50.0)
    remaining=max(0,goal['target']-goal['saved']); rng=np.random.default_rng(42); cm=[]
    for _ in range(n_simulations):
        mr=max(10.0,rng.normal(avg_save,avg_save*0.25)); cm.append(math.ceil(remaining/mr) if mr>0 else 999)
    cm=np.array(cm)
    if goal.get('deadline'):
        try:
            dl=datetime.strptime(goal['deadline'],"%Y-%m-%d"); ma=max(1,(dl-datetime.now()).days//30)
            prob=float(np.mean(cm<=ma)*100)
        except: prob=50.0
    else: prob=75.0
    pct=min(100,goal['saved']/goal['target']*100) if goal['target']>0 else 0
    status="On Track" if prob>=70 else ("At Risk" if prob>=40 else "Behind")
    return {"probability":round(prob,1),"months_p25":int(np.percentile(cm,25)),
            "months_p50":int(np.percentile(cm,50)),"months_p75":int(np.percentile(cm,75)),
            "avg_monthly_save":round(avg_save,2),"status":status,"pct_complete":round(pct,1),
            "save_trend":trend,"save_confidence":conf}

# ═══════════════════════════════════════════════════════════════════════════
#  ML: TRAJECTORY FORECAST
# ═══════════════════════════════════════════════════════════════════════════
def financial_trajectory_forecast(transactions,months_ahead=6):
    spend_txs=[t for t in transactions if t['type']=='Spend']
    dep_txs=[t for t in transactions if t['type']=='Deposit']
    if len(spend_txs)<3: return {"months":[],"insight":"Need more data.","trend":"unknown"}
    mspend=defaultdict(float); mincome=defaultdict(float)
    for t in spend_txs: mspend[t['date'][:7]]+=t['amount']
    for t in dep_txs: mincome[t['date'][:7]]+=t['amount']
    sv=[v for _,v in sorted(mspend.items())]; iv=[v for _,v in sorted(mincome.items())]
    sp,sc,st=weighted_sequence_predict(sv); ip,_,_=weighted_sequence_predict(iv) if iv else (50000,50,"stable")
    acc=get_account(); future=[]
    for i in range(1,months_ahead+1):
        td=datetime.now()+timedelta(days=30*i); sf=1+0.05*math.sin(2*math.pi*i/6)
        ps=max(0,sp*sf*(1+0.02*i)); pi=max(0,ip*(1+0.01*i))
        future.append({"month":td.strftime("%b %Y"),"projected_spend":round(ps,2),
                        "projected_income":round(pi,2),"projected_savings":round(max(0,pi-ps),2),"confidence":max(20,sc-i*5)})
    nw=acc['main_balance']; nwt=[]
    for m in future: nw+=m['projected_savings']; nwt.append({"month":m['month'],"net_worth":round(nw,2)})
    return {"months":future,"net_worth_trajectory":nwt,"spend_trend":st,"confidence":sc,
            "insights":["⚠️ Spending trending up" if st=="rising" else "✅ Spending declining" if st=="falling" else "📊 Spending stable"]}

# ═══════════════════════════════════════════════════════════════════════════
#  AI NOTIFICATIONS ENGINE
# ═══════════════════════════════════════════════════════════════════════════
def generate_smart_notifications(account,transactions,goals):
    notifs=[]
    spend_txs=[t for t in transactions if t['type']=='Spend']
    cat_spend=defaultdict(float)
    for t in spend_txs: cat_spend[t['category']]+=t['amount']
    if cat_spend.get("Food",0)>3000:
        notifs.append({"type":"spending","priority":"warning","title":"Food Spending Alert",
                        "message":f"₹{cat_spend['Food']:.0f} on food — {cat_spend['Food']/max(sum(cat_spend.values()),1)*100:.0f}% of total."})
    subs=detect_subscriptions(transactions); low=[s for s in subs if s.get('usage_score',50)<40]
    if low: notifs.append({"type":"subscription","priority":"info","title":"Subscription Review",
                            "message":f"{len(low)} low-usage subs. Save ₹{sum(s['amount'] for s in low):.0f}/mo by cancelling."})
    for g in goals:
        if g['target']>0:
            pct=g['saved']/g['target']*100
            if pct>=90: notifs.append({"type":"goal","priority":"success","title":"Goal Almost Done! 🎉","message":f"'{g['name']}' is {pct:.0f}% complete!"})
    if account['main_balance']<2000:
        notifs.append({"type":"balance","priority":"urgent","title":"Low Balance Warning","message":f"Balance is ₹{account['main_balance']:.2f}. Add funds soon."})
    anoms=[t for t in transactions if t.get('is_anomaly',0)==1]
    if anoms: notifs.append({"type":"fraud","priority":"urgent","title":f"⚠️ {len(anoms)} Unusual Transaction(s)","message":f"₹{anoms[0]['amount']} to {anoms[0]['receiver']} flagged."})
    return notifs

# ═══════════════════════════════════════════════════════════════════════════
#  AI ADVISOR (enhanced — affordability engine)
# ═══════════════════════════════════════════════════════════════════════════
def rule_based_advisor(message,transactions,account,health_score=None,goals=None):
    if goals is None: goals=[]
    msg=message.lower()
    spend_txs=[t for t in transactions if t['type']=='Spend']
    total_spend=sum(t['amount'] for t in spend_txs)
    cat_spend=defaultdict(float)
    for t in spend_txs: cat_spend[t['category']]+=t['amount']
    balance=account['main_balance']; piggy=account['piggy_bank']
    top_cat=max(cat_spend,key=cat_spend.get) if cat_spend else "N/A"
    dep_txs=[t for t in transactions if t['type']=='Deposit']
    mi_d=defaultdict(float)
    for t in dep_txs: mi_d[t['date'][:7]]+=t['amount']
    monthly_income=sum(mi_d.values())/len(mi_d) if mi_d else 50000

    # Affordability
    afford_match=re.search(r'[₹rs.]?\s*(\d[\d,]*)',msg)
    if any(w in msg for w in ["afford","can i buy","should i buy","purchase for"]):
        amount_str=afford_match.group(1).replace(',','') if afford_match else "0"
        try: amount=float(amount_str)
        except: amount=50000
        monthly_free=monthly_income-total_spend/max(1,len({t['date'][:7] for t in spend_txs}))
        if amount<=balance*0.3:
            return f"✅ Yes! You can comfortably afford ₹{amount:,.0f}.\n\nBalance: ₹{balance:,.0f} | This is {amount/balance*100:.0f}% of balance.\n💡 Pay from savings rather than reducing goal contributions."
        elif monthly_free>0:
            months=math.ceil(amount/monthly_free)
            suggested=max(monthly_free*0.5,amount/18)
            return f"🤔 Possible, but plan it:\n• Balance: ₹{balance:,.0f}\n• Monthly free cash: ₹{monthly_free:,.0f}\n• Months to save: {months}\n• Suggested save: ₹{suggested:,.0f}/month → {math.ceil(amount/suggested)} months\n\n{'✅ Affordable now from savings' if amount<=balance else '⏳ Save up first'}"
        else:
            return f"❌ Not recommended now. Monthly free cash is low (₹{monthly_free:,.0f}). Build emergency fund first."

    if any(w in msg for w in ["health","score","grade","wellness"]):
        if health_score:
            hs=health_score; lines="\n".join(f"• {v['label']}: {v['score']}/{v['max']} — {v['tip']}" for v in hs['pillars'].values())
            return f"🏥 HEALTH: {hs['total_score']}/100 (Grade {hs['grade']} — {hs['status']})\n\n{lines}\n\n💡 {hs['top_tip']}"
        return "Run health score from dashboard."
    elif any(w in msg for w in ["credit","cibil"]):
        cs=simulate_credit_score(account,transactions,goals)
        return f"📊 CREDIT SCORE: {cs['score']}/900 ({cs['rating']})\n\nTips:\n"+"\n".join(f"• {t}" for t in cs['tips'][:3])
    elif any(w in msg for w in ["anomaly","fraud","suspicious"]):
        anomalies=detect_anomalies(transactions)
        if anomalies:
            lines="\n".join(f"• ₹{a['amount']} → {a['receiver']} ({a['date'][:10]})" for a in anomalies[:3])
            return f"🚨 {len(anomalies)} flagged:\n{lines}"
        return "✅ No anomalies detected."
    elif any(w in msg for w in ["budget","allocat","limit"]):
        budget=smart_budget_allocate(monthly_income,transactions)
        lines="\n".join(f"• {v['emoji']} {cat}: ₹{v['allocated']:.0f}/mo ({v['trend']})" for cat,v in budget.items() if v['allocated']>200)
        return f"📋 SMART BUDGET (₹{monthly_income:.0f}/mo):\n{lines}"
    elif any(w in msg for w in ["recommend","merchant","suggest","where"]):
        recs=merchant_recommendations(transactions)
        if recs['recommendations']:
            lines="\n".join(f"• {r['name']} ({r['category']}) — {r['reason']}" for r in recs['recommendations'][:4])
            return f"🏪 RECOMMENDATIONS:\n{lines}"
        return "Make more transactions for recommendations."
    elif any(w in msg for w in ["subscript"]):
        subs=detect_subscriptions(transactions)
        if subs:
            total=sum(s['amount'] for s in subs)
            lines="\n".join(f"• {s['name']}: ₹{s['amount']}/mo — {s['recommendation']}" for s in subs)
            return f"📱 SUBSCRIPTIONS (₹{total:.0f}/mo):\n{lines}"
        return "No subscriptions detected yet."
    elif any(w in msg for w in ["save","saving"]):
        tips=[]
        if cat_spend.get("Food",0)>2000: tips.append("Reduce food ₹500/mo → ₹6K/year")
        if piggy>0: tips.append(f"Move piggy bank (₹{piggy:.2f}) to a goal")
        if not tips: tips=["Enable round-ups","Set category budgets"]
        return "💰 SAVING TIPS:\n"+"\n".join(f"• {t}" for t in tips)
    elif any(w in msg for w in ["hello","hi","help"]):
        return ("👋 VORTEx AI — Ask me:\n• 'Can I afford ₹80,000?'\n• health score | credit score\n• fraud check | smart budget\n• recommendations | subscriptions | save more")
    else:
        pct=(cat_spend.get(top_cat,0)/total_spend*100) if total_spend>0 else 0
        hs_line=f"Health: {health_score['total_score']}/100 ({health_score['grade']})\n" if health_score else ""
        return f"🤖 SNAPSHOT:\n{hs_line}Balance: ₹{balance:.2f} | Spent: ₹{total_spend:.0f}\nTop: {top_cat} ({pct:.0f}%)\n\nTry: 'afford ₹50000', 'credit score', 'smart budget'"

# ═══════════════════════════════════════════════════════════════════════════
#  REAL-TIME POST-TRANSACTION ENGINE (runs in background thread)
# ═══════════════════════════════════════════════════════════════════════════
def post_transaction_engine(amount,category,receiver,is_anomaly,anom_score):
    def _run():
        time.sleep(0.06)
        txs=get_transactions(200); acc=get_account()
        conn=get_db(); c=conn.cursor(); c.execute("SELECT * FROM goals"); goals=[dict(r) for r in c.fetchall()]; conn.close()

        # 1. Live health score
        hs=compute_health_score(acc,txs,goals)
        sse_push("health_update",{"score":hs['total_score'],"grade":hs['grade'],"status":hs['status'],
                                   "pillars":{k:{"score":v['score'],"max":v['max'],"tip":v['tip']} for k,v in hs['pillars'].items()}})

        # 2. Fraud alert
        if is_anomaly:
            sse_push("fraud_alert",{"amount":amount,"category":category,"receiver":receiver,"score":anom_score,
                                     "message":f"⚠️ Unusual: ₹{amount} to {receiver} (risk: {anom_score})"})
            add_notification("fraud","⚠️ Suspicious Transaction",f"₹{amount} to {receiver} flagged (score:{anom_score})","urgent")

        # 3. Budget tracker update
        current_month=datetime.now().strftime("%Y-%m")
        cat_spend=defaultdict(float)
        for t in txs:
            if t['type']=='Spend' and t['date'][:7]==current_month:
                cat_spend[t['category']]+=t['amount']
        conn2=get_db(); c2=conn2.cursor(); c2.execute("SELECT * FROM budget_limits")
        limits={row['category']:dict(row) for row in c2.fetchall()}; conn2.close()
        budget_status={}
        for cat,spent in cat_spend.items():
            limit=limits.get(cat,{}).get('monthly_limit',5000) or 5000
            pct=round(spent/limit*100,1)
            budget_status[cat]={"spent":round(spent,2),"limit":limit,"pct":pct,"over":spent>limit}
            if pct>=80 and cat==category:
                add_notification("budget",f"Budget: {cat}",f"Used {pct:.0f}% of ₹{limit} {cat} budget.","warning")
        sse_push("budget_update",budget_status)

        # 4. Account update
        sse_push("account_update",{"main_balance":acc['main_balance'],"piggy_bank":acc['piggy_bank'],
                                    "anomaly_count":sum(1 for t in txs if t.get('is_anomaly',0)==1)})

        # 5. Prediction update
        spends=[t['amount'] for t in txs if t['type']=='Spend']
        if len(spends)>2:
            pred,conf,trend=weighted_sequence_predict(spends)
            sse_push("prediction_update",{"next_spend":pred,"confidence":conf,"trend":trend})

        # 6. AI notifications
        notifs=generate_smart_notifications(acc,txs,goals)
        if notifs: sse_push("ai_notifications",{"notifications":notifs[:3]})

    threading.Thread(target=_run,daemon=True).start()

# ═══════════════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/login', methods=['POST'])
def login():
    d=request.json
    if d.get('username')=='admin' and d.get('password')=='password':
        return jsonify({"success":True,"db":"PostgreSQL" if USE_PG else "SQLite","ml":ML_AVAILABLE})
    return jsonify({"success":False,"error":"Invalid credentials"}),401

@app.route('/api/state')
def get_state():
    acc=get_account(); txs=get_transactions()
    spends=[t['amount'] for t in txs if t['type']=='Spend']
    analytics=defaultdict(float)
    for t in txs:
        if t['type']=='Spend': analytics[t['category']]+=t['amount']
    pv,pc,pt=weighted_sequence_predict(spends)
    prediction=f"₹{pv:,.0f} ({pt}, {pc}% conf)" if spends else "Need more data"
    ai_offer="Make your first transaction for AI insights!"
    if analytics: top=max(analytics,key=analytics.get); ai_offer=f"Top: {top} (₹{analytics[top]:.0f}) — try budgeting it!"
    conn=get_db(); c=conn.cursor()
    c.execute("SELECT * FROM milestones WHERE done=0 ORDER BY id LIMIT 1"); active_m=c.fetchone()
    c.execute("SELECT * FROM goals"); goals=[dict(r) for r in c.fetchall()]; conn.close()
    hs=compute_health_score(acc,txs,goals); anom_count=sum(1 for t in txs if t.get('is_anomaly',0)==1)
    current_month=datetime.now().strftime("%Y-%m"); cat_month=defaultdict(float)
    for t in txs:
        if t['type']=='Spend' and t['date'][:7]==current_month: cat_month[t['category']]+=t['amount']
    conn2=get_db(); c2=conn2.cursor(); c2.execute("SELECT * FROM budget_limits")
    limits={row['category']:dict(row) for row in c2.fetchall()}
    c2.execute("SELECT COUNT(*) as cnt FROM notifications WHERE is_read=0"); r2=c2.fetchone(); notif_count=r2['cnt'] if r2 else 0
    conn2.close()
    budget_status={}
    for cat,spent in cat_month.items():
        limit=limits.get(cat,{}).get('monthly_limit',5000) or 5000
        budget_status[cat]={"spent":round(spent,2),"limit":limit,"pct":round(spent/limit*100,1)}
    return jsonify({"account":acc,"transactions":txs,"active_milestone":dict(active_m) if active_m else None,
                    "prediction":prediction,"ai_offer":ai_offer,"analytics":dict(analytics),
                    "health_score":hs,"anomaly_count":anom_count,"budget_status":budget_status,
                    "notif_count":notif_count,"db_mode":"PostgreSQL" if USE_PG else "SQLite"})

@app.route('/api/add_money', methods=['POST'])
def add_money():
    d=request.json; amount=float(d.get('amount',0)); method=d.get('method','Bank Transfer')
    if amount<=0: return jsonify({"error":"Invalid amount"}),400
    acc=get_account(); update_account(main=acc['main_balance']+amount)
    add_transaction("Deposit",amount,method,"Main Account")
    sse_push("account_update",{"main_balance":acc['main_balance']+amount,"piggy_bank":acc['piggy_bank']})
    add_notification("deposit","💰 Funds Added",f"₹{amount:,.0f} added via {method}","success")
    return jsonify({"success":True})

@app.route('/api/spend', methods=['POST'])
def spend_money():
    d=request.json; amount=float(d.get('amount',0)); category=d.get('category','') or ''
    upi_id=d.get('upi_id','Unknown'); receiver=d.get('receiver_name','Merchant'); round_to=int(d.get('round_to',10))
    if round_to not in [10,100]: round_to=10
    if amount<=0: return jsonify({"error":"Invalid amount"}),400
    if not category or category=='auto': category=classify_transaction(receiver,amount)
    all_txs=get_transactions(); is_anom,anom_score=flag_new_transaction(amount,category,all_txs)
    tx_class=classify_transaction(receiver,amount)
    platform_fee=1.0 if amount>50 else 0.0; rounded=math.ceil(amount/float(round_to))*round_to; spare=rounded-amount
    total_ded=amount+platform_fee+spare; acc=get_account()
    if acc['main_balance']<total_ded: return jsonify({"error":"Insufficient funds"}),400
    update_account(main=acc['main_balance']-total_ded,piggy=acc['piggy_bank']+spare)
    add_transaction("Spend",amount,category,receiver,upi_id,int(is_anom),tx_class)
    if platform_fee>0: add_transaction("Fee",platform_fee,"Platform Fee","FinPay Network")
    if spare>0: add_transaction("Round-Up",round(spare,2),f"Saved (₹{round_to})","Piggy Bank")
    conn=get_db(); c=conn.cursor(); p=db_ph()
    c.execute("SELECT * FROM milestones WHERE done=0 ORDER BY id LIMIT 1"); m=c.fetchone()
    if m and (m['category']=='Any' or m['category']==category):
        new_prog=m['progress']+amount
        if new_prog>=m['target']:
            c.execute(f"UPDATE milestones SET done=1,progress={p} WHERE id={p}",(new_prog,m['id']))
            cash_r=random.randint(1,9); acc2=get_account(); update_account(piggy=acc2['piggy_bank']+cash_r)
            add_transaction("Cashback",cash_r,"Reward",f"₹{cash_r} Cash Reward!")
            sse_push("milestone_complete",{"reward":cash_r,"desc":m['desc']})
        else: c.execute(f"UPDATE milestones SET progress={p} WHERE id={p}",(new_prog,m['id']))
    conn.commit(); conn.close()
    if spare>0:
        c2=get_db(); cu=c2.cursor()
        cu.execute("SELECT * FROM goals WHERE saved<target ORDER BY id LIMIT 1"); goal=cu.fetchone()
        if goal: cu.execute(f"UPDATE goals SET saved=saved+{p} WHERE id={p}",(spare,goal['id'])); c2.commit()
        c2.close()
    post_transaction_engine(amount,category,receiver,is_anom,anom_score)
    return jsonify({"success":True,"anomaly_detected":is_anom,"anomaly_score":anom_score,"auto_category":tx_class})

@app.route('/api/donate', methods=['POST'])
def donate():
    d=request.json; amount=float(d.get('amount',0)); charity=d.get('charity','General Fund')
    acc=get_account()
    if amount<=0 or acc['main_balance']<amount: return jsonify({"error":"Insufficient funds"}),400
    update_account(main=acc['main_balance']-amount)
    add_transaction("Donation",amount,"Charity",charity)
    return jsonify({"success":True})

@app.route('/api/redeem', methods=['POST'])
def redeem():
    acc=get_account()
    if acc['piggy_bank']<=0: return jsonify({"error":"Empty"}),400
    amount=acc['piggy_bank']; update_account(main=acc['main_balance']+amount,piggy=0.0)
    add_transaction("Redeem",round(amount,2),"Piggy Bank to Main","Self")
    sse_push("account_update",{"main_balance":acc['main_balance']+amount,"piggy_bank":0})
    return jsonify({"success":True})

@app.route('/api/transactions')
def get_txs(): return jsonify(get_transactions(500))

@app.route('/api/budget/limits', methods=['GET'])
def get_budget_limits():
    conn=get_db(); c=conn.cursor(); c.execute("SELECT * FROM budget_limits"); rows=[dict(r) for r in c.fetchall()]; conn.close()
    return jsonify(rows)

@app.route('/api/budget/limits', methods=['POST'])
def set_budget_limit():
    d=request.json; cat=d.get('category'); limit=float(d.get('limit',0)); p=db_ph()
    conn=get_db(); c=conn.cursor()
    if USE_PG: c.execute(f"INSERT INTO budget_limits(category,monthly_limit) VALUES({p},{p}) ON CONFLICT(category) DO UPDATE SET monthly_limit={p}",(cat,limit,limit))
    else: c.execute(f"INSERT OR REPLACE INTO budget_limits(category,monthly_limit) VALUES({p},{p})",(cat,limit))
    conn.commit(); conn.close(); return jsonify({"success":True})

@app.route('/api/goals')
def get_goals():
    conn=get_db(); c=conn.cursor(); p=db_ph()
    c.execute("SELECT * FROM goals ORDER BY id DESC"); goals=[dict(r) for r in c.fetchall()]
    c.execute("SELECT * FROM transactions ORDER BY id DESC"); txs=[dict(r) for r in c.fetchall()]; conn.close()
    for g in goals:
        g['progress_pct']=round(min(g['saved']/g['target']*100,100),1) if g['target'] else 0
        if g.get('deadline'):
            try:
                dl=datetime.strptime(g['deadline'],"%Y-%m-%d"); ml=max(1,(dl-datetime.now()).days//30)
                g['monthly_needed']=round((g['target']-g['saved'])/ml,2); g['months_left']=ml
            except: g['monthly_needed']=0; g['months_left']=0
        else: g['monthly_needed']=0; g['months_left']=0
        g['forecast']=goal_achievement_forecast(g,txs); g['optimizer']=optimize_goal(g,txs)
    return jsonify(goals)

@app.route('/api/goals/create', methods=['POST'])
def create_goal():
    d=request.json; name=d.get('name','My Goal'); target=float(d.get('target',0))
    deadline=d.get('deadline',''); emoji=d.get('emoji','🎯')
    if target<=0: return jsonify({"error":"Invalid target"}),400
    conn=get_db(); cu=conn.cursor(); p=db_ph()
    cu.execute(f"INSERT INTO goals(name,target,saved,deadline,created_at,emoji) VALUES({p},{p},0,{p},{p},{p})",(name,target,deadline,now_str(),emoji))
    conn.commit(); conn.close(); sse_push("goal_created",{"name":name,"target":target}); return jsonify({"success":True})

@app.route('/api/goals/contribute', methods=['POST'])
def contribute_goal():
    d=request.json; gid=int(d.get('goal_id',0)); amount=float(d.get('amount',0))
    acc=get_account()
    if amount<=0 or acc['main_balance']<amount: return jsonify({"error":"Insufficient funds"}),400
    conn=get_db(); cu=conn.cursor(); p=db_ph()
    cu.execute(f"SELECT * FROM goals WHERE id={p}",(gid,)); goal=cu.fetchone()
    if not goal: conn.close(); return jsonify({"error":"Not found"}),404
    cu.execute(f"UPDATE goals SET saved=saved+{p} WHERE id={p}",(amount,gid)); conn.commit(); conn.close()
    update_account(main=acc['main_balance']-amount)
    add_transaction("GoalSave",amount,"Goal Saving",goal['name'])
    g=dict(goal); new_saved=g['saved']+amount; pct=min(100,new_saved/g['target']*100)
    sse_push("goal_update",{"id":gid,"name":g['name'],"saved":new_saved,"pct":round(pct,1)})
    if pct>=100: add_notification("goal","🎉 Goal Achieved!",f"'{g['name']}' 100% complete!","success")
    return jsonify({"success":True})

@app.route('/api/goals/remove', methods=['DELETE'])
def remove_goal():
    gid=request.json.get('goal_id'); conn=get_db(); cu=conn.cursor(); p=db_ph()
    cu.execute(f"DELETE FROM goals WHERE id={p}",(gid,)); conn.commit(); conn.close(); return jsonify({"success":True})

@app.route('/api/goals/optimize', methods=['POST'])
def optimize_goal_ep():
    d=request.json; gid=int(d.get('goal_id',0)); income=float(d.get('monthly_income',0))
    conn=get_db(); cu=conn.cursor(); p=db_ph()
    cu.execute(f"SELECT * FROM goals WHERE id={p}",(gid,)); goal=cu.fetchone(); conn.close()
    if not goal: return jsonify({"error":"Not found"}),404
    return jsonify(optimize_goal(dict(goal),get_transactions(200),income))

@app.route('/api/simulate/car', methods=['POST'])
def simulate_car():
    d=request.json; price=float(d.get('price',0)); down=float(d.get('down_payment',0))
    tenure=float(d.get('tenure_years',5)); rate=float(d.get('interest_rate',9.5))/100; income=float(d.get('monthly_income',50000))
    principal=price-down; n=int(tenure*12); r=rate/12
    emi=principal*r*(1+r)**n/((1+r)**n-1) if r>0 else principal/n
    total=emi*n+down; interest=total-price; impact=round((emi/income)*100,1)
    risk="HIGH" if impact>40 else("MEDIUM" if impact>25 else "LOW"); score=max(0,100-int(impact*1.5))
    return jsonify({"emi":round(emi,2),"total_payment":round(total,2),"total_interest":round(interest,2),"savings_impact":impact,"emergency_risk":risk,"financial_score":score,"tenure_months":n})

@app.route('/api/simulate/investment', methods=['POST'])
def simulate_investment():
    d=request.json; monthly=float(d.get('monthly_amount',5000)); years=float(d.get('years',10))
    rate=float(d.get('annual_return',12.0))/100; r=rate/12; n=int(years*12)
    fv=monthly*((1+r)**n-1)/r*(1+r) if r>0 else monthly*n; invested=monthly*n; returns=fv-invested
    yearly=[{"year":yr,"value":round(monthly*((1+r)**(yr*12)-1)/r*(1+r) if r>0 else monthly*yr*12,2),"invested":round(monthly*yr*12,2)} for yr in range(1,int(years)+1)]
    return jsonify({"future_value":round(fv,2),"total_invested":round(invested,2),"total_returns":round(returns,2),"return_pct":round(returns/invested*100,1) if invested else 0,"yearly_data":yearly})

@app.route('/api/simulate/life-decision', methods=['POST'])
def simulate_life_decision():
    d=request.json; acc=get_account(); txs=get_transactions(200)
    conn=get_db(); cu=conn.cursor(); cu.execute("SELECT * FROM goals"); goals=[dict(r) for r in cu.fetchall()]; conn.close()
    return jsonify(life_decision_simulate(d.get('type','car'),d.get('params',{}),acc,txs,goals))

@app.route('/api/ml/health-score')
def ml_health_score():
    acc=get_account(); txs=get_transactions()
    conn=get_db(); cu=conn.cursor(); cu.execute("SELECT * FROM goals"); goals=[dict(r) for r in cu.fetchall()]; conn.close()
    return jsonify(compute_health_score(acc,txs,goals))

@app.route('/api/ml/anomalies')
def ml_anomalies():
    txs=get_transactions(500); anoms=detect_anomalies(txs); return jsonify({"anomalies":anoms,"total":len(anoms)})

@app.route('/api/ml/budget', methods=['POST'])
def ml_budget():
    d=request.json; income=float(d.get('income',50000)); return jsonify(smart_budget_allocate(income,get_transactions(200)))

@app.route('/api/ml/forecast')
def ml_forecast():
    txs=get_transactions(200); rf=rf_spending_prediction(txs); cat_preds=predict_by_category_weighted(txs)
    all_spends=[t['amount'] for t in txs if t['type']=='Spend']; overall,conf,trend=weighted_sequence_predict(all_spends)
    return jsonify({"by_category":cat_preds,"overall":{"prediction":overall,"confidence":conf,"trend":trend},"rf_prediction":rf,"model":"GBM+WExp+Ridge"})

@app.route('/api/ml/behavioral')
def ml_behavioral():
    spend_txs=[t for t in get_transactions(300) if t['type']=='Spend']
    if len(spend_txs)<6 or not ML_AVAILABLE: return jsonify({"clusters":[],"insight":"Need 6+ transactions.","patterns":[]})
    try:
        features=[]
        for t in spend_txs:
            try: dt=datetime.strptime(t['date'],"%Y-%m-%d %H:%M"); features.append([dt.hour,t['amount'],dt.weekday()])
            except: pass
        if len(features)<6: return jsonify({"clusters":[],"insight":"Need more data.","patterns":[]})
        X=np.array(features,dtype=float); scaler=StandardScaler(); Xs=scaler.fit_transform(X)
        k=min(3,len(features)//2); km=KMeans(n_clusters=k,random_state=42,n_init=10); labels=km.fit_predict(Xs)
        cluster_info=[]
        for ci in range(k):
            c_txs=[spend_txs[i] for i in range(min(len(spend_txs),len(labels))) if labels[i]==ci]
            if not c_txs: continue
            amounts=[t['amount'] for t in c_txs]; hours=[]
            for t in c_txs:
                try: hours.append(datetime.strptime(t['date'],"%Y-%m-%d %H:%M").hour)
                except: pass
            ah=sum(hours)/len(hours) if hours else 12; aa=sum(amounts)/len(amounts)
            tl=("Morning" if ah<10 else "Lunch" if ah<14 else "Afternoon" if ah<18 else "Evening/Night")+" spender"
            il="High-value" if aa>1000 else ("Mid-range" if aa>300 else "Micro")
            cats=defaultdict(int)
            for t in c_txs: cats[t['category']]+=1
            cluster_info.append({"cluster_id":ci,"size":len(c_txs),"avg_amount":round(aa,2),"avg_hour":round(ah,1),"label":f"{il} · {tl}","top_category":max(cats,key=cats.get)})
        patterns=[]
        if any(c['avg_hour']>=22 or c['avg_hour']<=6 for c in cluster_info): patterns.append("🌙 Late-night spending detected")
        categories=defaultdict(float)
        for t in spend_txs: categories[t['category']]+=t['amount']
        if categories:
            top=max(categories,key=categories.get); pct=categories[top]/sum(categories.values())*100
            if pct>40: patterns.append(f"📊 {top} is {pct:.0f}% of spending")
        return jsonify({"clusters":cluster_info,"insight":f"Found {k} spending patterns.","patterns":patterns})
    except Exception as e:
        return jsonify({"clusters":[],"insight":str(e),"patterns":[]})

@app.route('/api/ml/trajectory')
def ml_trajectory():
    months=int(request.args.get('months',6)); return jsonify(financial_trajectory_forecast(get_transactions(200),months))

@app.route('/api/ml/montecarlo', methods=['POST'])
def ml_montecarlo():
    d=request.json
    return jsonify(monte_carlo_simulate(float(d.get('monthly_income',50000)),float(d.get('monthly_expense',35000)),int(d.get('months',12)),int(d.get('n_simulations',500))))

@app.route('/api/ml/classify', methods=['POST'])
def ml_classify():
    d=request.json; return jsonify({"category":classify_transaction(d.get('receiver',''),float(d.get('amount',0)))})

@app.route('/api/ml/behavior-score')
def ml_behavior_score():
    acc=get_account(); txs=get_transactions(200)
    conn=get_db(); cu=conn.cursor(); cu.execute("SELECT * FROM goals"); goals=[dict(r) for r in cu.fetchall()]; conn.close()
    return jsonify(compute_behavior_score(acc,txs,goals))

@app.route('/api/ml/credit-score')
def ml_credit_score():
    acc=get_account(); txs=get_transactions(200)
    conn=get_db(); cu=conn.cursor(); cu.execute("SELECT * FROM goals"); goals=[dict(r) for r in cu.fetchall()]; conn.close()
    return jsonify(simulate_credit_score(acc,txs,goals))

@app.route('/api/ml/recommendations')
def ml_recommendations():
    return jsonify(merchant_recommendations(get_transactions(200)))

@app.route('/api/ml/notifications')
def ml_notifications():
    acc=get_account(); txs=get_transactions(200)
    conn=get_db(); cu=conn.cursor(); cu.execute("SELECT * FROM goals"); goals=[dict(r) for r in cu.fetchall()]; conn.close()
    return jsonify({"notifications":generate_smart_notifications(acc,txs,goals)})

@app.route('/api/notifications')
def get_notifications():
    conn=get_db(); cu=conn.cursor(); cu.execute("SELECT * FROM notifications ORDER BY id DESC LIMIT 50")
    rows=[dict(r) for r in cu.fetchall()]; conn.close(); return jsonify(rows)

@app.route('/api/notifications/read', methods=['POST'])
def mark_read():
    nid=request.json.get('id'); p=db_ph(); conn=get_db(); cu=conn.cursor()
    if nid: cu.execute(f"UPDATE notifications SET is_read=1 WHERE id={p}",(nid,))
    else: cu.execute("UPDATE notifications SET is_read=1")
    conn.commit(); conn.close(); return jsonify({"success":True})

@app.route('/api/subscriptions')
def get_subscriptions():
    txs=get_transactions(500); subs=detect_subscriptions(txs)
    total=sum(s['amount'] for s in subs if s['frequency'] in ('Monthly','Recurring'))
    annual=sum(s['annual_cost'] for s in subs); low=[s for s in subs if s.get('usage_score',50)<40]
    potential=sum(s['amount'] for s in low); tip=f"Review {len(low)} low-usage subs to save ₹{potential:.0f}/mo" if low else ""
    conn=get_db(); cu=conn.cursor(); cu.execute("SELECT * FROM subscriptions_managed ORDER BY id DESC")
    managed=[dict(r) for r in cu.fetchall()]; conn.close()
    return jsonify({"subscriptions":subs,"total_monthly":round(total,2),"annual_cost":round(annual,2),"savings_tip":tip,"potential_savings":round(potential,2),"managed":managed})

@app.route('/api/subscriptions/add', methods=['POST'])
def add_managed_sub():
    d=request.json; p=db_ph(); conn=get_db(); cu=conn.cursor()
    cu.execute(f"INSERT INTO subscriptions_managed(name,amount,frequency,next_date,usage_score,notes,merchant_upi) VALUES({p},{p},{p},{p},{p},{p},{p})",
               (d.get('name'),float(d.get('amount',0)),d.get('frequency','Monthly'),d.get('next_date',''),int(d.get('usage_score',50)),d.get('notes',''),d.get('merchant_upi','')))
    conn.commit(); conn.close(); return jsonify({"success":True})

@app.route('/api/subscriptions/update', methods=['POST'])
def update_sub():
    d=request.json; sid=int(d.get('id',0)); p=db_ph(); conn=get_db(); cu=conn.cursor()
    cu.execute(f"UPDATE subscriptions_managed SET is_active={p},usage_score={p},notes={p} WHERE id={p}",
               (int(d.get('is_active',1)),int(d.get('usage_score',50)),d.get('notes',''),sid))
    conn.commit(); conn.close(); return jsonify({"success":True})

@app.route('/api/subscriptions/delete', methods=['DELETE'])
def delete_sub():
    sid=request.json.get('id'); p=db_ph(); conn=get_db(); cu=conn.cursor()
    cu.execute(f"DELETE FROM subscriptions_managed WHERE id={p}",(sid,)); conn.commit(); conn.close(); return jsonify({"success":True})

@app.route('/api/ai-advisor/chat', methods=['POST'])
def ai_chat():
    d=request.json; message=d.get('message',''); txs=get_transactions(200); acc=get_account()
    conn=get_db(); cu=conn.cursor(); cu.execute("SELECT * FROM goals"); goals=[dict(r) for r in cu.fetchall()]; conn.close()
    hs=compute_health_score(acc,txs,goals); reply=rule_based_advisor(message,txs,acc,hs,goals)
    return jsonify({"reply":reply,"timestamp":now_str()})

@app.route('/api/analytics')
def analytics():
    txs=get_transactions(500)
    monthly=defaultdict(float); cat_totals=defaultdict(float); savings_trend=defaultdict(float)
    for t in txs:
        if t['type']=='Spend': monthly[t['date'][:7]]+=t['amount']; cat_totals[t['category']]+=t['amount']
        if t['type'] in ('Round-Up','GoalSave','Cashback'): savings_trend[t['date'][:7]]+=t['amount']
    return jsonify({"monthly_spending":dict(sorted(monthly.items())),"category_breakdown":dict(sorted(cat_totals.items(),key=lambda x:-x[1])),"savings_trend":dict(sorted(savings_trend.items()))})

@app.route('/api/merchants')
def get_merchants():
    conn=get_db(); cu=conn.cursor(); cu.execute("SELECT * FROM merchants"); rows=[dict(r) for r in cu.fetchall()]; conn.close()
    return jsonify(rows)

@app.route('/api/pay', methods=['POST'])
def pay_merchant():
    d=request.json; upi=d.get('upi_id',''); amount=float(d.get('amount',0))
    if amount<=0: return jsonify({"error":"Invalid amount"}),400
    conn=get_db(); cu=conn.cursor(); p=db_ph()
    cu.execute(f"SELECT * FROM merchants WHERE upi={p}",(upi,)); m=cu.fetchone(); conn.close()
    name=dict(m)['name'] if m else "Merchant"; cat=dict(m)['cat'] if m else classify_transaction(upi,amount)
    acc=get_account()
    if acc['main_balance']<amount: return jsonify({"error":"Insufficient funds"}),400
    spare=math.ceil(amount/10)*10-amount; all_txs=get_transactions(); is_anom,asc=flag_new_transaction(amount,cat,all_txs)
    update_account(main=acc['main_balance']-amount-spare,piggy=acc['piggy_bank']+spare)
    add_transaction("Spend",amount,cat,name,upi,int(is_anom),cat)
    if spare>0: add_transaction("Round-Up",round(spare,2),"Saved (₹10)","Piggy Bank")
    post_transaction_engine(amount,cat,name,is_anom,asc)
    return jsonify({"success":True,"merchant":name,"category":cat,"anomaly":is_anom})

if __name__=='__main__':
    init_db()
    print(f"\n{'='*55}\n🚀 VORTEx FinPay v4\n   DB:  {'PostgreSQL ✅' if USE_PG else 'SQLite (set DATABASE_URL for PG)'}\n   ML:  {'Active ✅' if ML_AVAILABLE else 'Unavailable'}\n   SSE: Real-time push ✅\n   URL: http://localhost:5000\n{'='*55}\n")
    app.run(debug=False,host='0.0.0.0',port=5000,threaded=True)