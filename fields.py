# Key paths observed in logs/raw/* (STEP 2, mint 7Vce7BaVkxjcp7Ua23Hpe1ixW4YYzWaAgt7m3fhypump). None = not present.
# DexScreener paths are relative to one pair object in the top-level list; P = the pumpswap pair (when indexed).
FIELDS = {
 "pumpportal": {"mint": "mint", "pool": "pool", "signature": "signature", "creator": None, "txType": "txType"},
 "dexscreener": {
  "pairAddress": "[i].pairAddress", "dexId": "[i].dexId", "priceUsd": "[i].priceUsd",
  "liquidity_usd": None,  # [i].liquidity.usd absent on the pumpfun-curve pair in this sample
  "marketCap": "[i].marketCap", "fdv": "[i].fdv",
  "volume_m5": "[i].volume.m5", "volume_h1": "[i].volume.h1", "volume_h24": "[i].volume.h24",
  "txns_m5_buys": "[i].txns.m5.buys", "txns_m5_sells": "[i].txns.m5.sells",
  "txns_h1_buys": "[i].txns.h1.buys", "txns_h1_sells": "[i].txns.h1.sells",
  "priceChange_m5": "[i].priceChange.m5", "priceChange_h1": "[i].priceChange.h1", "priceChange_h24": "[i].priceChange.h24",
  "pairCreatedAt": "[i].pairCreatedAt", "baseMint": "[i].baseToken.address"},
 "rugcheck_report": {
  "creator": "creator", "creatorBalance": "creatorBalance",
  "market_pool": "markets[i].pubkey", "market_type": "markets[i].marketType",
  "lp_locked_pct": "markets[i].lp.lpLockedPct", "lp_burned_pct": None,
  "lp_locked_usd": "markets[i].lp.lpLockedUSD",
  "pool_vault_owner": "markets[i].liquidityAAccount.owner",
  "top_holders": "topHolders", "holder_owner": "topHolders[i].owner", "holder_pct": "topHolders[i].pct",
  "holder_insider": "topHolders[i].insider",
  "known_account_type": "knownAccounts.<addr>.type",  # AMM / CREATOR / LOCKER; match vs topHolders[i].owner to tag pool/curve
  "mintAuthority": "token.mintAuthority", "freezeAuthority": "token.freezeAuthority",
  "supply": "token.supply", "decimals": "token.decimals",
  "totalHolders": "totalHolders", "graphInsidersDetected": "graphInsidersDetected", "rugged": "rugged", "score": "score"},
 "rugcheck_insiders_graph": {"nodes": None, "edges": None},  # response was [] for this mint
 "rpc_getAccountInfo": {
  "mintAuthority": "result.value.data.parsed.info.mintAuthority",
  "freezeAuthority": "result.value.data.parsed.info.freezeAuthority",
  "supply": "result.value.data.parsed.info.supply", "decimals": "result.value.data.parsed.info.decimals",
  "owner_program": "result.value.owner"},
}
