//+------------------------------------------------------------------+
//|  Реальный мост: советник читает команды и файл состояния,         |
//|  обменивается с Python через общую папку MQL5\Files (FILE_COMMON).|
//|  Никакого WebRequest — только файлы, поэтому не нужны галочки.    |
//|                                                                  |
//|  Файл команды — это приказ, который пережил того, кто его        |
//|  написал. Python убирает его, когда разрешения нет, но этот       |
//|  советник не может этому верить: он проверяет сам, что команда   |
//|  называет ЕГО счёт, что она не просрочена и что лот не больше     |
//|  потолка. Закрывает только свои лоты (по magic).                  |
//+------------------------------------------------------------------+
#property copyright "DSH"
#property version   "1.01"
#property strict

input string CmdFile   = "dsh_real_cmd.json";
input string StateFile = "dsh_real_state.json";
input int    TimerMs   = 1000;
// Тот же номер, что REAL_MAGIC в scripts/real_bridge.py: им помечен каждый наш лот.
input long   InpMagic  = 8272026;
input double MaxLots   = 0.01;      // потолок одного открываемого лота
input int    MaxCommandTtlSec = 600; // верхняя граница срока команды: она не может быть вечной

string ReadAll(const string name)
{
   int handle = FileOpen(name, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return "";
   string text = "";
   while(!FileIsEnding(handle))
      text += FileReadString(handle) + "\n";
   FileClose(handle);
   return text;
}

void WriteAll(const string name, const string text)
{
   int handle = FileOpen(name, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
      return;
   FileWriteString(handle, text);
   FileClose(handle);
}

string JsonStr(const string src, const string key)
{
   string needle = "\"" + key + "\":\"";
   int start = StringFind(src, needle);
   if(start < 0)
      return "";
   start += StringLen(needle);
   int end = StringFind(src, "\"", start);
   if(end < 0)
      return "";
   return StringSubstr(src, start, end - start);
}

double JsonNum(const string src, const string key)
{
   string needle = "\"" + key + "\":";
   int start = StringFind(src, needle);
   if(start < 0)
      return 0.0;
   start += StringLen(needle);
   string rest = StringSubstr(src, start);
   StringTrimLeft(rest);
   int pos = 0;
   string token = "";
   while(pos < StringLen(rest))
   {
      ushort ch = StringGetCharacter(rest, pos);
      if((ch >= '0' && ch <= '9') || ch == '.' || ch == '-')
      {
         token += ShortToString(ch);
         pos++;
      }
      else
         break;
   }
   return StringToDouble(token);
}

void SendState()
{
   string body = "{\"time\":" + IntegerToString((int)TimeLocal()) +
                 ",\"connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false") +
                 ",\"login\":" + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) +
                 ",\"server\":\"" + AccountInfoString(ACCOUNT_SERVER) + "\"" +
                 ",\"company\":\"" + AccountInfoString(ACCOUNT_COMPANY) + "\"" +
                 ",\"currency\":\"" + AccountInfoString(ACCOUNT_CURRENCY) + "\"" +
                 ",\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
                 ",\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2) +
                 ",\"margin_free\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) +
                 ",\"trade_allowed\":" + (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "true" : "false") +
                 ",\"positions\":[";
   int total = PositionsTotal();
   bool first = true;
   for(int i = 0; i < total; i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(!first)
         body += ",";
      first = false;
      body += "{\"ticket\":" + IntegerToString((long)ticket) +
              ",\"magic\":" + IntegerToString((long)PositionGetInteger(POSITION_MAGIC)) +
              ",\"symbol\":\"" + PositionGetString(POSITION_SYMBOL) + "\"" +
              ",\"type\":" + IntegerToString((int)PositionGetInteger(POSITION_TYPE)) +
              ",\"volume\":" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 2) +
              ",\"price\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN), 5) +
              ",\"profit\":" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 2) +
              ",\"sl\":" + DoubleToString(PositionGetDouble(POSITION_SL), 5) +
              ",\"tp\":" + DoubleToString(PositionGetDouble(POSITION_TP), 5) + "}";
   }
   body += "]}";
   WriteAll(StateFile, body);
}

bool OpenTrade(const string symbol, const int side, const double lots,
               const double sl, const double tp, const double stop_pct, const string comment)
{
   MqlTradeRequest request;
   MqlTradeResult  result;
   ZeroMemory(request);
   ZeroMemory(result);
   request.action   = TRADE_ACTION_DEAL;
   request.symbol   = symbol;
   request.volume   = lots;
   request.type     = (side == 1) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   request.deviation = 50;
   request.magic    = InpMagic;
   request.comment  = comment;
   request.type_filling = ORDER_FILLING_FOK;
   double price = (side == 1) ? SymbolInfoDouble(symbol, SYMBOL_BID)
                              : SymbolInfoDouble(symbol, SYMBOL_ASK);
   request.price = price;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   // The percentage is the bridge's way of saying "this lot stops somewhere" without knowing the
   // price it will fill at. Resolving it here, against the price actually chosen, is what makes the
   // stop the broker's rather than this process's next cycle: a lot whose only exit is a Python loop
   // survives as long as that loop does.
   double stop = sl;
   if(stop <= 0 && stop_pct > 0)
      stop = (side == 1) ? price * (1.0 + stop_pct / 100.0) : price * (1.0 - stop_pct / 100.0);
   if(stop > 0)
      request.sl = NormalizeDouble(stop, digits);
   if(tp > 0)
      request.tp = NormalizeDouble(tp, digits);
   if(!OrderSend(request, result))
   {
      Print("order failed: ", result.retcode, " ", result.comment);
      return false;
   }
   Print("order ok: ", result.retcode, " deal ", result.deal);
   return true;
}

bool ClosePosition(const ulong ticket)
{
   if(!PositionSelectByTicket(ticket))
      return false;
   // Билет в команде — это только номер. На неттинговом счёте под ним может стоять ручной лот
   // оператора, и закрыть его по чужой команде значит потратить его деньги. Magic — единственный
   // признак, что этот лот открыл мост.
   if(PositionGetInteger(POSITION_MAGIC) != InpMagic)
   {
      Print("BLOCKED: билет ", ticket, " открыт не нами (magic=",
            PositionGetInteger(POSITION_MAGIC), ") — закрыто не будет");
      return false;
   }
   MqlTradeRequest request;
   MqlTradeResult  result;
   ZeroMemory(request);
   ZeroMemory(result);
   string symbol = PositionGetString(POSITION_SYMBOL);
   long   type   = PositionGetInteger(POSITION_TYPE);
   double volume = PositionGetDouble(POSITION_VOLUME);
   request.action   = TRADE_ACTION_DEAL;
   request.position = ticket;
   request.symbol   = symbol;
   request.volume   = volume;
   request.type     = (type == POSITION_TYPE_BUY) ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   request.deviation = 50;
   request.magic    = InpMagic;
   request.comment  = "bridge close";
   request.type_filling = ORDER_FILLING_FOK;
   request.price = (type == POSITION_TYPE_BUY) ? SymbolInfoDouble(symbol, SYMBOL_BID)
                                               : SymbolInfoDouble(symbol, SYMBOL_ASK);
   if(!OrderSend(request, result))
   {
      Print("close failed: ", result.retcode, " ", result.comment);
      return false;
   }
   Print("close ok: ", result.retcode);
   return true;
}

void ProcessCommand()
{
   string raw = ReadAll(CmdFile);
   if(StringLen(raw) < 10)
      return;
   FileDelete(CmdFile, FILE_COMMON);
   // Команда лежит в общей папке, где её может прочитать любой прикреплённый советник, и она
   // переживает тот цикл, который её написал. Значит проверяем здесь, а не полагаетесь на Python:
   // счёт назван этот же? срок ещё не вышел? лот не больше потолка?
   double login  = JsonNum(raw, "login");
   double exp    = JsonNum(raw, "exp");
   double now_epoch = (double)TimeGMT();
   if(login <= 0 || MathFloor(login) != login || login != (double)AccountInfoInteger(ACCOUNT_LOGIN))
   {
      Print("BLOCKED: команда называет другой счёт (login=", login, "), этот терминал — ",
            AccountInfoInteger(ACCOUNT_LOGIN));
      return;
   }
   if(exp <= 0 || MathFloor(exp) != exp)
   {
      Print("BLOCKED: у команды нет целочисленного срока; файл, переживший разрешение, не приказ.");
      return;
   }
   if(exp <= now_epoch)
   {
      Print("BLOCKED: команда просрочена, ни один ордер не отправлен.");
      return;
   }
   if(exp > now_epoch + MaxCommandTtlSec)
   {
      Print("BLOCKED: срок команды дальше, чем ", MaxCommandTtlSec, " секунд.");
      return;
   }
   string action = JsonStr(raw, "action");
   string symbol = JsonStr(raw, "symbol");
   double lots   = JsonNum(raw, "lots");
   double sl     = JsonNum(raw, "sl");
   double tp     = JsonNum(raw, "tp");
   double side   = JsonNum(raw, "side");
   double ticket = JsonNum(raw, "ticket");
   double stop_pct = JsonNum(raw, "stop_pct");
   if(action == "open" && StringLen(symbol) > 0 && lots > 0)
   {
      if(lots > MaxLots)
      {
         Print("BLOCKED: лот ", lots, " больше потолка советника ", MaxLots);
         return;
      }
      // An open with nothing protecting it is not a small order: it is a position that can only be
      // closed by whichever process remembers to close it. The bridge that sends without a level is
      // refusing on purpose, and so is this one.
      if(sl <= 0 && tp <= 0 && stop_pct <= 0)
      {
         Print("BLOCKED: открытие без защитного уровня (sl/tp/stop_pct пусты) — на реальных деньгах "
               "так не делается");
         return;
      }
      OpenTrade(symbol, (int)side, lots, sl, tp, stop_pct, "bridge open");
   }
   else if(action == "close" && ticket > 0)
      ClosePosition((ulong)ticket);
}

int OnInit()
{
   EventSetTimer(1);
   Print("DSH real bridge started, common files: ", CmdFile, " / ", StateFile);
   SendState();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
}

void OnTimer()
{
   ProcessCommand();
   SendState();
}
