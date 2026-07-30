"""
Gemini Decision & Veto Engine.
Uses the official google-genai SDK to validate candidate trades against news
catalysts, technical context, and quantitative filters.
"""

import json
from google import genai
from google.genai import types
from config import GEMINI_API_KEY, GEMINI_MODEL

# Initialize standard GenAI Client
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def evaluate_trade_candidate(ticker, news_items, indicator_snapshot, account_snapshot):
    """
    Evaluates candidate trades using Gemini as a Veto/Validation Agent.
    Returns a structured dictionary indicating whether to execute the trade.
    """
    if not client:
        print("[Decide Engine] Gemini API key missing. Skipping model veto.")
        return {"approve": False, "reasoning": "Missing Gemini API key"}

    # Construct quantitative summary prompt
    prompt = f"""
    You are an automated quantitative trading veto agent reviewing trade proposals.
    
    Target Ticker: {ticker}
    Current Price: ${indicator_snapshot.get('price')}
    
    Technical Snapshot:
    - Trend: {indicator_snapshot.get('trend')}
    - RSI (14): {indicator_snapshot.get('rsi')}
    - SMA20: {indicator_snapshot.get('sma20')} | SMA50: {indicator_snapshot.get('sma50')}
    - Volume Trend: {indicator_snapshot.get('volume_trend_pct')}%
    - MACD Histogram: {indicator_snapshot.get('macd', {}).get('histogram') if indicator_snapshot.get('macd') else 'N/A'}
    
    Recent News Catalysts:
    {json.dumps(news_items[:3], indent=2)}
    
    Account Portfolio Snapshot:
    - Available Cash: ${account_snapshot.get('cash'):,.2f}
    - Total Portfolio Value: ${account_snapshot.get('total_value'):,.2f}
    - Current Position: {account_snapshot.get('holdings').get(ticker, 'None')}

    INSTRUCTIONS:
    Analyze if the news catalyst presents a strong, genuine market catalyst (earnings, FDA approval, major acquisition, major contract) 
    that aligns with technical trend indicators. Veto/reject low-impact, speculative, or conflicting news.
    
    Respond STRICTLY with valid JSON format matching this schema:
    {{
        "approve": true or false,
        "action": "BUY", "SELL", or "HOLD",
        "confidence_score": float between 0.0 and 1.0,
        "reasoning": "Short single-sentence concise justification."
    }}
    """

    try:
        # Request structured JSON output using Google GenAI SDK
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )

        parsed_response = json.loads(response.text)
        return parsed_response

    except Exception as e:
        print(f"[Decide Engine] Error generating decision for {ticker}: {e}")
        return {
            "approve": False,
            "action": "HOLD",
            "confidence_score": 0.0,
            "reasoning": f"Validation error: {str(e)}",
        }
