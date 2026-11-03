import json,asyncio,time,urllib.request,websockets
R="https://api.mainnet-beta.solana.com";D="logs/raw/"
def post(m,p,t=60):
    rq=urllib.request.Request(R,json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),{"Content-Type":"application/json"})
    return urllib.request.urlopen(rq,timeout=t).read().decode()
def get(u):
    return urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"Mozilla/5.0"}),timeout=30).read().decode()
def sv(n,s): open(D+n,"w").write(s)
s=post("getSlot",[]);sv("rpc_getSlot.json",s);print("slot",s)
async def ws():
    async with websockets.connect("wss://pumpportal.fun/api/data") as w:
        await w.send(json.dumps({"method":"subscribeMigration"}))
        end=time.time()+300
        while time.time()<end:
            m=await asyncio.wait_for(w.recv(),end-time.time())
            j=json.loads(m)
            if "mint" in j: return m
            print("non-mig",m[:200])
m=asyncio.run(ws());sv("pumpportal_migration.json",m);mint=json.loads(m)["mint"];print("mint",mint);print(m[:600])
d=get(f"https://api.dexscreener.com/tokens/v1/solana/{mint}");sv("dexscreener_token.json",d)
print("dex",[(p.get("dexId"),p.get("pairAddress")) for p in json.loads(d)])
for n,u in [("rugcheck_report.json","report"),("rugcheck_insiders_graph.json","insiders/graph")]:
    try: r=get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/{u}");sv(n,r);print(n,"ok",len(r))
    except Exception as e: print(n,"ERR",e)
a=post("getAccountInfo",[mint,{"encoding":"jsonParsed"}]);sv("rpc_getAccountInfo_mint.json",a);print(a[:800])
open("mint.txt","w").write(mint)
for n,prog,f in [("token",'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',[{"dataSize":165},{"memcmp":{"offset":0,"bytes":mint}}]),("token2022",'TokenzQdBNbLqP5VEhdkAS6EAuyZ2R5PNctfDjSTSsQ',[{"memcmp":{"offset":0,"bytes":mint}}])]:
    for i in range(3):
        try:
            r=post("getProgramAccounts",[prog,{"encoding":"jsonParsed","filters":f}],90);sv(f"rpc_gpa_{n}.json",r);j=json.loads(r)
            print(n,"count",len(j["result"]) if "result" in j else "ERROR "+json.dumps(j.get("error")));break
        except Exception as e: print(n,"try",i,"EXC",repr(e)[:300]);time.sleep(2)
