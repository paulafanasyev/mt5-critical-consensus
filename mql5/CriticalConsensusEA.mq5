#property strict
#property version   "0.12"
#property description "Demo-only reference bridge. Disabled by default; live execution is blocked."

input string BridgeUrl = "http://127.0.0.1:8000/v1/execution/next";
input string KillSwitchUrl = "http://127.0.0.1:8000/v1/risk/kill-switch";
input string BridgeToken = ""; // For local testing only; use a proper secret manager/short-lived signing in production.
input bool EnableDemoExecution = false;
input bool AllowLiveExecution = false; // Template intentionally does not permit live execution even if set true.
input int PollSeconds = 3;
input int MaxSlippagePoints = 20;
input long MagicNumber = 8172026;
input double MaxVolume = 0.20;
input double MaxCommandTtlSeconds = 600; // Upper bound on expires_at_epoch - now.

// Key/value slots collected by the most recent ValidateFlatObject() call. Lookups read only
// these slots, never the raw body, so a string value that happens to contain a field name can
// never be mistaken for that field. Callers stop on failure, so a partial list is never used.
string g_validated_keys[];
string g_validated_values[];
int    g_validated_types[]; // 1=string, 2=number, 3=true, 4=false

int FindValidatedKey(const string key)
{
   for(int i = 0; i < ArraySize(g_validated_keys); i++)
      if(g_validated_keys[i] == key) return i;
   return -1;
}

bool IsJsonDigits(const string text, const int start, const int end)
{
   if(end <= start) return false;
   for(int i = start; i < end; i++)
   {
      ushort c = StringGetCharacter(text, i);
      if(c < '0' || c > '9') return false;
   }
   return true;
}

// Strict RFC-8259 number token: no leading zeros, no bare signs, no duplicate separators.
bool IsJsonNumberToken(const string text)
{
   int n = StringLen(text);
   if(n == 0) return false;
   int i = 0;
   if(StringGetCharacter(text, 0) == '-') i++;
   int int_start = i;
   while(i < n && StringGetCharacter(text, i) >= '0' && StringGetCharacter(text, i) <= '9') i++;
   if(!IsJsonDigits(text, int_start, i)) return false;
   if(i - int_start > 1 && StringGetCharacter(text, int_start) == '0') return false;
   if(i < n && StringGetCharacter(text, i) == '.')
   {
      i++;
      int frac_start = i;
      while(i < n && StringGetCharacter(text, i) >= '0' && StringGetCharacter(text, i) <= '9') i++;
      if(!IsJsonDigits(text, frac_start, i)) return false;
   }
   if(i < n && (StringGetCharacter(text, i) == 'e' || StringGetCharacter(text, i) == 'E'))
   {
      i++;
      if(i < n && (StringGetCharacter(text, i) == '+' || StringGetCharacter(text, i) == '-')) i++;
      int exp_start = i;
      while(i < n && StringGetCharacter(text, i) >= '0' && StringGetCharacter(text, i) <= '9') i++;
      if(!IsJsonDigits(text, exp_start, i)) return false;
   }
   return i == n;
}

void StoreValidatedValue(const string key, const string value, const int value_type)
{
   int index = ArraySize(g_validated_keys);
   ArrayResize(g_validated_keys, index + 1);
   ArrayResize(g_validated_values, index + 1);
   ArrayResize(g_validated_types, index + 1);
   g_validated_keys[index] = key;
   g_validated_values[index] = value;
   g_validated_types[index] = value_type;
}

// This deliberately small parser supports only flat JSON objects with scalar values, as emitted by a
// schema-validated localhost bridge. It is NOT a general JSON parser. Production must replace it with
// a vetted MQL5 JSON library.
bool IsJsonWs(const ushort c)
{
   return (c==' ' || c=='\t' || c=='\r' || c=='\n');
}

void SkipJsonWs(const string s, int &i, const int n)
{
   while(i < n && IsJsonWs(StringGetCharacter(s,i))) i++;
}

bool KeyInList(const string key) { return FindValidatedKey(key) >= 0; }

// Accepts exactly one flat object with string/number/true/false values. Rejects nesting, arrays,
// escapes, control characters, duplicate keys and any trailing content, so a later key lookup can
// never resolve to text hidden inside a value. Escaped or null values are refused on purpose: an
// unusual field makes the EA block, never guess.
bool ValidateFlatObject(const string json, string &error)
{
   error = "";
   ArrayResize(g_validated_keys, 0);
   ArrayResize(g_validated_values, 0);
   ArrayResize(g_validated_types, 0);
   string s = json;
   StringTrimLeft(s);
   StringTrimRight(s);
   int n = StringLen(s);
   if(n < 2 || StringGetCharacter(s,0) != '{' || StringGetCharacter(s,n-1) != '}')
   {
      error = "body must be exactly one JSON object";
      return false;
   }
   int i = 1;
   bool after_comma = false;
   while(true)
   {
      SkipJsonWs(s, i, n);
      if(i >= n) { error = "unterminated object"; return false; }
      ushort c = StringGetCharacter(s,i);
      if(c == '}')
      {
         if(i != n-1) { error = "trailing content after object"; return false; }
         if(after_comma) { error = "trailing comma"; return false; }
         return true;
      }
      if(c == '{' || c == '[') { error = "nested object or array rejected"; return false; }
      if(c != '"') { error = "object key must be a string"; return false; }

      i++; // opening quote of the key
      int key_start = i;
      while(i < n && StringGetCharacter(s,i) != '"')
      {
         ushort kc = StringGetCharacter(s,i);
         if(kc == '\\') { error = "escape sequences rejected"; return false; }
         if(kc < 0x20) { error = "control character rejected"; return false; }
         i++;
      }
      if(i >= n) { error = "unterminated object key"; return false; }
      string key = StringSubstr(s, key_start, i-key_start);
      if(StringLen(key) == 0) { error = "empty object key"; return false; }
      if(KeyInList(key))
      {
         error = "DUPLICATE JSON KEY: " + key;
         return false;
      }
      ArrayResize(g_validated_keys, ArraySize(g_validated_keys)+1);
      g_validated_keys[ArraySize(g_validated_keys)-1] = key;
      i++; // closing quote of the key

      SkipJsonWs(s, i, n);
      if(i >= n || StringGetCharacter(s,i) != ':') { error = "missing ':' after key"; return false; }
      i++;
      SkipJsonWs(s, i, n);
      if(i >= n) { error = "missing value"; return false; }
      c = StringGetCharacter(s,i);
      if(c == '{' || c == '[') { error = "nested value rejected"; return false; }
      if(c == '"')
      {
         i++;
         int value_start = i;
         while(i < n && StringGetCharacter(s,i) != '"')
         {
            ushort vc = StringGetCharacter(s,i);
            if(vc == '\\') { error = "escape sequences rejected"; return false; }
            if(vc < 0x20) { error = "control character rejected"; return false; }
            i++;
         }
         if(i >= n) { error = "unterminated string value"; return false; }
         StoreValidatedValue(key, StringSubstr(s, value_start, i - value_start), 1);
         i++;
      }
      else if(c == 't' || c == 'f')
      {
         string token = "false";
         if(c == 't') token = "true";
         int tl = StringLen(token);
         // The literal must be complete and followed by a delimiter, never a prefix match.
         if(i + tl >= n || StringSubstr(s, i, tl) != token)
         { error = "invalid boolean literal"; return false; }
         StoreValidatedValue(key, token, c == 't' ? 3 : 4);
         i += tl;
      }
      else if((c >= '0' && c <= '9') || c == '-')
      {
         int number_start = i;
         while(i < n)
         {
            ushort nc = StringGetCharacter(s,i);
            if(nc == ',' || nc == '}' || IsJsonWs(nc)) break;
            i++;
         }
         string number_token = StringSubstr(s, number_start, i - number_start);
         if(!IsJsonNumberToken(number_token) || !MathIsValidNumber(StringToDouble(number_token)))
         { error = "invalid number literal"; return false; }
         StoreValidatedValue(key, number_token, 2);
      }
      else { error = "unsupported value type"; return false; }

      SkipJsonWs(s, i, n);
      if(i >= n) { error = "unterminated object"; return false; }
      c = StringGetCharacter(s,i);
      if(c == ',') { i++; after_comma = true; continue; }
      if(c == '}')
      {
         if(i != n-1) { error = "trailing content after object"; return false; }
         return true;
      }
      error = "unexpected character after value";
      return false;
   }
}

// Callers must run ValidateFlatObject() first. Lookups return values from the validated slots
// only, so text inside a string value can never be interpreted as another field.
string JsonString(const string key)
{
   int index = FindValidatedKey(key);
   if(index < 0 || g_validated_types[index] != 1) return "";
   return g_validated_values[index];
}

double JsonNumber(const string key)
{
   int index = FindValidatedKey(key);
   if(index < 0 || g_validated_types[index] != 2) return 0.0;
   return StringToDouble(g_validated_values[index]);
}

// The type slot records an exact literal, so "trueish" or "false_positive" cannot match.
bool JsonBoolExact(const string key, const bool wanted)
{
   int index = FindValidatedKey(key);
   if(index < 0) return false;
   return g_validated_types[index] == (wanted ? 3 : 4);
}

bool JsonTrue(const string key) { return JsonBoolExact(key, true); }
bool JsonFalse(const string key) { return JsonBoolExact(key, false); }

// Identifiers come from a schema-validated command, so only a narrow printable alphabet is
// accepted. This also rejects control characters before they reach logs or file names.
bool IsSafeIdentifier(const string value)
{
   int n = StringLen(value);
   if(n < 1 || n > 128) return false;
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(value, i);
      bool allowed = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
                     || c == '-' || c == '_' || c == '.' || c == ':';
      if(!allowed) return false;
   }
   return true;
}

// FNV-1a over the full identifier, rendered as 16 hex digits. Truncating identifiers into one
// 63-character key made distinct IDs collide; hashing both separately keeps both bindings.
// The offset basis is assembled from two 32-bit halves because a single 64-bit literal exceeds
// the signed range accepted by older MQL5 compilers.
string Fnv1aHex(const string text)
{
   ulong hash = ((ulong)0xCBF29CE4 << 32) | (ulong)0x84222325;
   int n = StringLen(text);
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(text, i);
      hash ^= (ulong)(c & 0xFF);
      hash *= 1099511628211;
      hash ^= (ulong)((c >> 8) & 0xFF);
      hash *= 1099511628211;
   }
   return StringFormat("%016I64X", (long)hash);
}

// Two tombstones are written: command_id and client_order_id are both bound, and the receiver
// still must not trust client_order_id alone. FNV is not cryptographic; the bridge's exact-ID
// record and broker reconciliation remain authoritative.
string CommandTombstoneKey(const string command_id) { return "CC_TRIED_C_" + Fnv1aHex(command_id); }
string OrderTombstoneKey(const string order_id) { return "CC_TRIED_O_" + Fnv1aHex(order_id); }

// The token becomes an HTTP header value, so only a narrow ASCII alphabet is accepted. This
// blocks CR/LF header injection and any accidental whitespace or non-ASCII paste.
bool IsValidBridgeToken(const string token)
{
   int n = StringLen(token);
   if(n < 24 || n > 256) return false;
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(token, i);
      bool allowed = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z')
                     || c == '.' || c == '_' || c == '~' || c == '-';
      if(!allowed) return false;
   }
   return true;
}

// A prefix check is not enough: "http://127.0.0.1:8000@attacker.example" passes a naive
// StringFind test. This parser requires the exact loopback host, an explicit numeric port,
// and rejects userinfo, query/fragment separators, backslashes, whitespace and non-ASCII.
bool IsLoopbackHttpUrl(const string url, string &error)
{
   error = "";
   int n = StringLen(url);
   if(n < 17) { error = "URL is too short"; return false; }
   if(StringSubstr(url, 0, 7) != "http://") { error = "URL must start with http://"; return false; }
   for(int i = 0; i < n; i++)
   {
      ushort c = StringGetCharacter(url, i);
      if(c < 0x21 || c > 0x7E) { error = "URL contains whitespace, control or non-ASCII"; return false; }
      if(c == '@' || c == '#' || c == '?' || c == '\\') { error = "URL contains a forbidden character"; return false; }
   }
   int host_start = 7;
   int colon = StringFind(url, ":", host_start);
   if(colon < 0) { error = "URL must include an explicit port"; return false; }
   string host = StringSubstr(url, host_start, colon - host_start);
   if(host != "127.0.0.1" && host != "localhost")
   { error = "URL host must be exactly 127.0.0.1 or localhost"; return false; }
   int path = StringFind(url, "/", colon + 1);
   string port_text = (path < 0) ? StringSubstr(url, colon + 1) : StringSubstr(url, colon + 1, path - colon - 1);
   int plen = StringLen(port_text);
   if(plen < 1 || plen > 5) { error = "URL port must have 1..5 digits"; return false; }
   if(StringGetCharacter(port_text, 0) == '0') { error = "URL port must not have a leading zero"; return false; }
   long port = 0;
   for(int i = 0; i < plen; i++)
   {
      ushort c = StringGetCharacter(port_text, i);
      if(c < '0' || c > '9') { error = "URL port must be numeric"; return false; }
      port = port * 10 + (c - '0');
   }
   if(port < 1 || port > 65535) { error = "URL port is out of range"; return false; }
   if(path >= 0 && path == n - 1) { error = "URL path is empty after '/'"; return false; }
   return true;
}

bool CheckKillSwitch()
{
   if(!IsValidBridgeToken(BridgeToken) || StringLen(KillSwitchUrl) == 0)
   {
      Print("BLOCKED: bridge token or kill-switch URL is invalid.");
      return false;
   }
   char data[];
   char result[];
   string result_headers = "";
   string headers = "Accept: application/json\r\n";
   headers += "X-Bridge-Key: " + BridgeToken + "\r\n";
   ResetLastError();
   int code = WebRequest("GET", KillSwitchUrl, headers, 3000, data, result, result_headers);
   if(code != 200)
   {
      Print("BLOCKED: kill-switch state unavailable; HTTP ", code, ", error=", GetLastError());
      return false;
   }
   string response = CharArrayToString(result, 0, -1, CP_UTF8);
   string parse_error = "";
   if(!ValidateFlatObject(response, parse_error))
   {
      Print("BLOCKED: kill-switch response is not a flat JSON object: ", parse_error);
      return false;
   }
   if(!KeyInList("kill_switch_active"))
   {
      Print("BLOCKED: kill-switch response has no kill_switch_active field.");
      return false;
   }
   if(!JsonFalse("kill_switch_active"))
   {
      Print("BLOCKED: kill switch is active or state is invalid.");
      return false;
   }
   return true;
}

bool FetchCommand(string &body)
{
   char data[];
   char result[];
   string result_headers = "";
   string headers = "Accept: application/json\r\n";
   if(!IsValidBridgeToken(BridgeToken))
   {
      Print("BLOCKED: BridgeToken is empty or malformed; no command request sent.");
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
   string parse_error = "";
   if(!ValidateFlatObject(body, parse_error))
   {
      Print("BLOCKED: command body rejected: ", parse_error);
      return false;
   }

   // Contract with schemas/order_command.schema.json: every required key must be present.
   string required_keys[] = {"command_id", "client_order_id", "permission_id", "signal_id",
                             "instrument", "direction", "size", "expected_price", "sl", "tp",
                             "mode", "user_decision_id", "user_decision", "user_approved",
                             "expires_at_epoch", "payload_hash", "created_at"};
   for(int k = 0; k < ArraySize(required_keys); k++)
   {
      if(!KeyInList(required_keys[k]))
      {
         Print("BLOCKED: required OrderCommand field missing: ", required_keys[k]);
         return false;
      }
   }

   string command_id = JsonString("command_id");
   string order_id = JsonString("client_order_id");
   string symbol = JsonString("instrument");
   string direction = JsonString("direction");
   string mode = JsonString("mode");
   string decision = JsonString("user_decision");
   double volume = JsonNumber("size");
   double sl = JsonNumber("sl");
   double tp = JsonNumber("tp");
   double expected_price = JsonNumber("expected_price");
   double expires_raw = JsonNumber("expires_at_epoch");
   bool user_approved = JsonTrue("user_approved");

   if(command_id=="" || order_id=="" || symbol=="" || direction=="" || mode=="" || decision=="")
   { Print("BLOCKED: required OrderCommand field missing."); return false; }
   if(!IsSafeIdentifier(command_id) || !IsSafeIdentifier(order_id) || !IsSafeIdentifier(symbol))
   { Print("BLOCKED: command/order/symbol identifier contains unsupported characters."); return false; }
   // Direction must be one of the schema values before it reaches any price computation.
   if(direction != "BUY" && direction != "SELL")
   { Print("BLOCKED: unsupported direction: ", direction); return false; }
   if(mode != "demo" || decision != "APPROVE" || !user_approved)
   { Print("BLOCKED: demo mode and explicit user approval are required."); return false; }
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   { Print("BLOCKED: account is not demo. This EA does not execute live trades."); return false; }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
   { Print("BLOCKED: terminal or account trading is not allowed."); return false; }
   if(!EnableDemoExecution || AllowLiveExecution)
   { Print("BLOCKED: demo execution is disabled, or live mode was requested."); return false; }
   if(expires_raw <= 0 || MathFloor(expires_raw) != expires_raw)
   { Print("BLOCKED: expires_at_epoch is not a positive whole number."); return false; }
   double now_epoch = (double)TimeGMT();
   // Compare in doubles before casting so a huge value cannot overflow the long conversion.
   if(expires_raw <= now_epoch)
   { Print("BLOCKED: command expired."); return false; }
   if(expires_raw > now_epoch + MaxCommandTtlSeconds)
   { Print("BLOCKED: command TTL exceeds MaxCommandTtlSeconds; expires_at=", expires_raw); return false; }

   if(volume <= 0 || volume > MaxVolume || sl <= 0 || tp <= 0 || expected_price <= 0)
   { Print("BLOCKED: invalid volume/SL/TP/expected price."); return false; }

   string command_key = CommandTombstoneKey(command_id);
   string order_key = OrderTombstoneKey(order_id);
   if(GlobalVariableCheck(command_key) || GlobalVariableCheck(order_key))
   { Print("DUPLICATE BLOCKED: command or order already attempted."); return false; }

   if(!SymbolSelect(symbol, true)) { Print("BLOCKED: SymbolSelect failed for ",symbol); return false; }
   ENUM_SYMBOL_TRADE_MODE trade_mode = (ENUM_SYMBOL_TRADE_MODE)SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE);
   if(trade_mode != SYMBOL_TRADE_MODE_FULL)
   { Print("BLOCKED: symbol does not allow full trading (mode=", (int)trade_mode, ")."); return false; }
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

   long stops = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL);
   if(MathAbs(live_price - sl) < stops * point || MathAbs(tp - live_price) < stops * point)
   { Print("BLOCKED: SL/TP inside broker stops level (", stops, " points)."); return false; }

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

   // Filling mode must be one the broker advertises for this symbol.
   long filling = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((filling & (long)SYMBOL_FILLING_FOK) != 0)       request.type_filling = ORDER_FILLING_FOK;
   else if((filling & (long)SYMBOL_FILLING_IOC) != 0)  request.type_filling = ORDER_FILLING_IOC;
   else { Print("BLOCKED: symbol advertises neither FOK nor IOC for a market deal."); return false; }

   request.comment = "CC:" + StringSubstr(order_id,0,20);

   if(!OrderCheck(request, check))
   { Print("BLOCKED: OrderCheck failed; retcode=",check.retcode," ",check.comment); return false; }

   // Re-read the emergency stop in the last moment before the send; a switch flipped during
   // OrderCheck or the risk calculation must still stop this order.
   if(!CheckKillSwitch())
   { Print("BLOCKED: kill switch changed before send."); return false; }

   // At-most-once: both tombstones are written and flushed before the send. Terminal global
   // variables are not crash-atomic, so the bridge's exact-ID record and broker reconciliation
   // remain authoritative, and an uncertain result requires manual reconciliation, never a retry.
   // The tombstones are intentionally kept after a failed send because the order may still have
   // reached the broker.
   if(GlobalVariableSet(command_key, (double)TimeGMT()) == 0
      || GlobalVariableSet(order_key, (double)TimeGMT()) == 0)
   { Print("BLOCKED: cannot persist idempotency tombstones."); return false; }
   // The flush must be confirmed: a best-effort flush could leave the process before the
   // terminal persisted the tombstones, so a resend after a restart would not be blocked.
   if(!GlobalVariablesFlush())
   { Print("BLOCKED: cannot flush idempotency tombstones."); return false; }

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

int OnInit()
{
   // Fail closed: only exact loopback hosts with an explicit numeric port are permitted, no
   // warning-only path exists. An empty or malformed token must also refuse initialization;
   // an empty token would otherwise poll forever and log a BLOCKED line on every tick.
   string url_error = "";
   if(!IsLoopbackHttpUrl(BridgeUrl, url_error))
   {
      Print("BLOCKED: BridgeUrl rejected (", url_error, "); refusing to initialize.");
      return(INIT_FAILED);
   }
   if(!IsLoopbackHttpUrl(KillSwitchUrl, url_error))
   {
      Print("BLOCKED: KillSwitchUrl rejected (", url_error, "); refusing to initialize.");
      return(INIT_FAILED);
   }
   if(!IsValidBridgeToken(BridgeToken))
   {
      Print("BLOCKED: BridgeToken is empty or not 24..256 characters of [A-Za-z0-9._~-]; refusing to initialize.");
      return(INIT_FAILED);
   }
   EventSetTimer((int)MathMax(1, PollSeconds));
   Print("CriticalConsensusEA initialized. Demo execution default is disabled. Add bridge URL to MT5 WebRequest allowlist.");
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
{
   // Before each command poll, require a fresh explicit false from the persisted stop endpoint.
   if(!CheckKillSwitch()) return;
   string body = "";
   if(!FetchCommand(body)) return;
   ExecuteDemoCommand(body);
}
