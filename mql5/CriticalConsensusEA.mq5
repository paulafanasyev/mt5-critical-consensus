#property strict
#property version   "0.10"
#property description "Demo-only reference bridge. Disabled by default; live execution is blocked."

input string BridgeUrl = "http://127.0.0.1:8000/v1/execution/next";
input string BridgeToken = ""; // For local testing only; use a proper secret manager/short-lived signing in production.
input bool EnableDemoExecution = false;
input bool AllowLiveExecution = false; // Template intentionally does not permit live execution even if set true.
input int PollSeconds = 3;
input int MaxSlippagePoints = 20;
input long MagicNumber = 8172026;
input double MaxVolume = 0.20;

// This deliberately small parser supports only flat, trusted scalar JSON emitted by a schema-validated
// localhost bridge. It is NOT a general JSON parser. Production must replace it with a vetted MQL5 JSON library.
string JsonString(const string json, const string key)
{
   string needle = "\"" + key + "\"";
   int p = StringFind(json, needle);
   if(p < 0) return "";
   p = StringFind(json, ":", p + StringLen(needle));
   if(p < 0) return "";
   p++;
   while(p < StringLen(json) && (StringGetCharacter(json,p)==' ' || StringGetCharacter(json,p)=='\t')) p++;
   if(p >= StringLen(json) || StringGetCharacter(json,p)!='\"') return "";
   p++;
   int e = StringFind(json, "\"", p);
   if(e < 0) return "";
   return StringSubstr(json, p, e-p);
}

double JsonNumber(const string json, const string key)
{
   string needle = "\"" + key + "\"";
   int p = StringFind(json, needle);
   if(p < 0) return 0.0;
   p = StringFind(json, ":", p + StringLen(needle));
   if(p < 0) return 0.0;
   p++;
   while(p < StringLen(json) && (StringGetCharacter(json,p)==' ' || StringGetCharacter(json,p)=='\t')) p++;
   int e = p;
   while(e < StringLen(json))
   {
      ushort c = StringGetCharacter(json,e);
      if(c==',' || c=='}' || c==' ' || c=='\r' || c=='\n') break;
      e++;
   }
   if(e <= p) return 0.0;
   return StringToDouble(StringSubstr(json,p,e-p));
}

bool JsonTrue(const string json, const string key)
{
   string needle = "\"" + key + "\"";
   int p = StringFind(json, needle);
   if(p < 0) return false;
   p = StringFind(json, ":", p + StringLen(needle));
   if(p < 0) return false;
   string tail = StringSubstr(json, p+1, 12);
   StringTrimLeft(tail); StringTrimRight(tail);
   return StringFind(tail, "true") == 0;
}

string SafeGlobalKey(string command_id)
{
   StringReplace(command_id, "-", "_");
   StringReplace(command_id, ":", "_");
   StringReplace(command_id, ".", "_");
   if(StringLen(command_id) > 48) command_id = StringSubstr(command_id,0,48);
   return "CC_TRIED_" + command_id;
}

bool FetchCommand(string &body)
{
   char data[];
   char result[];
   string result_headers = "";
   string headers = "Accept: application/json\r\n";
   if(StringLen(BridgeToken)==0)
   {
      Print("BLOCKED: BridgeToken is empty; no command request sent.");
      return false;
   }
   headers += "X-Bridge-Key: " + BridgeToken + "\r\n";
   ResetLastError();
   int code = WebRequest("GET", BridgeUrl, headers, 3000, data, result, result_headers);
   if(code == -1)
   {
      Print("WebRequest failed; code=", GetLastError());
      return false;
   }
   if(code == 204 || code == 404) return false;
   if(code != 200)
   {
      Print("Bridge returned HTTP ", code, "; fail closed.");
      return false;
   }
   body = CharArrayToString(result, 0, -1, CP_UTF8);
   if(StringLen(body) < 10) return false;
   return true;
}

bool ExecuteDemoCommand(const string body)
{
   string command_id = JsonString(body, "command_id");
   string order_id = JsonString(body, "client_order_id");
   string symbol = JsonString(body, "instrument");
   string direction = JsonString(body, "direction");
   string mode = JsonString(body, "mode");
   string decision = JsonString(body, "user_decision");
   double volume = JsonNumber(body, "size");
   double sl = JsonNumber(body, "sl");
   double tp = JsonNumber(body, "tp");
   double expected_price = JsonNumber(body, "expected_price");
   long expires_at = (long)JsonNumber(body, "expires_at_epoch");
   bool user_approved = JsonTrue(body, "user_approved");

   if(command_id=="" || order_id=="" || symbol=="" || direction=="" || mode=="" || decision=="")
   { Print("BLOCKED: required OrderCommand field missing."); return false; }
   if(mode != "demo" || decision != "APPROVE" || !user_approved)
   { Print("BLOCKED: demo mode and explicit user approval are required."); return false; }
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   { Print("BLOCKED: account is not demo. This EA does not execute live trades."); return false; }
   if(!EnableDemoExecution || AllowLiveExecution)
   { Print("BLOCKED: demo execution is disabled, or live mode was requested."); return false; }
   if(expires_at <= (long)TimeGMT())
   { Print("BLOCKED: command expired."); return false; }
   if(volume <= 0 || volume > MaxVolume || sl <= 0 || tp <= 0 || expected_price <= 0)
   { Print("BLOCKED: invalid volume/SL/TP/expected price."); return false; }

   string global_key = SafeGlobalKey(order_id);
   if(GlobalVariableCheck(global_key))
   { Print("DUPLICATE BLOCKED: client_order_id already attempted: ", order_id); return false; }

   if(!SymbolSelect(symbol, true)) { Print("BLOCKED: SymbolSelect failed for ",symbol); return false; }
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick)) { Print("BLOCKED: no current tick."); return false; }
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(point <= 0) { Print("BLOCKED: invalid symbol point."); return false; }
   double live_price = (direction=="BUY") ? tick.ask : tick.bid;
   if(MathAbs(live_price - expected_price) > MaxSlippagePoints * point)
   { Print("BLOCKED: slippage guard triggered."); return false; }
   if(direction=="BUY" && !(sl < live_price && live_price < tp))
   { Print("BLOCKED: invalid BUY SL/TP geometry."); return false; }
   if(direction=="SELL" && !(tp < live_price && live_price < sl))
   { Print("BLOCKED: invalid SELL SL/TP geometry."); return false; }
   if(direction!="BUY" && direction!="SELL") { Print("BLOCKED: unsupported direction."); return false; }

   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double minvol = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxvol = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   if(step <= 0 || volume < minvol || volume > maxvol || MathAbs(volume/step - MathRound(volume/step)) > 0.000001)
   { Print("BLOCKED: broker volume constraints not satisfied."); return false; }

   MqlTradeRequest request = {};
   MqlTradeCheckResult check = {};
   MqlTradeResult result = {};
   request.action = TRADE_ACTION_DEAL;
   request.magic = MagicNumber;
   request.symbol = symbol;
   request.volume = volume;
   request.type = (direction=="BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   request.price = NormalizeDouble(live_price, digits);
   request.sl = NormalizeDouble(sl, digits);
   request.tp = NormalizeDouble(tp, digits);
   request.deviation = MaxSlippagePoints;
   request.type_time = ORDER_TIME_GTC;
   request.comment = "CC:" + StringSubstr(order_id,0,20);

   if(!OrderCheck(request, check))
   { Print("BLOCKED: OrderCheck failed; retcode=",check.retcode," ",check.comment); return false; }

   // At-most-once: record attempt before network operation. A timeout/uncertain result needs reconciliation,
   // never blind retry. A production bridge must persist the same idempotency key in PostgreSQL too.
   GlobalVariableSet(global_key, (double)TimeGMT());
   ResetLastError();
   bool sent = OrderSend(request, result);
   if(!sent || (result.retcode != TRADE_RETCODE_DONE && result.retcode != TRADE_RETCODE_DONE_PARTIAL && result.retcode != TRADE_RETCODE_PLACED))
   {
      Print("ORDER NOT CONFIRMED; manual reconciliation required. sent=",sent,
            " retcode=",result.retcode," error=",GetLastError()," comment=",result.comment);
      return false;
   }
   Print("Order accepted; reconcile actual fill/position before marking final. command_id=",command_id,
         " order_id=",order_id," order=",result.order," deal=",result.deal," retcode=",result.retcode);
   return true;
}

void OnInit()
{
   if(StringFind(BridgeUrl, "http://127.0.0.1:") != 0 && StringFind(BridgeUrl, "http://localhost:") != 0)
      Print("WARNING: Bridge URL is not localhost. Production must use authenticated/TLS network policy.");
   EventSetTimer(MathMax(1, PollSeconds));
   Print("CriticalConsensusEA initialized. Demo execution default is disabled. Add bridge URL to MT5 WebRequest allowlist.");
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
{
   string body = "";
   if(!FetchCommand(body)) return;
   ExecuteDemoCommand(body);
}
