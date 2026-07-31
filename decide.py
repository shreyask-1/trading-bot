import json
from google import genai
from config import GEMINI_API_KEY, GEMINI_MODEL

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else genai.Client()

def evaluate_entire_market(master_ticker_list):
    """
    Sends all market snapshots in a single JSON payload to Gemini 
    for instant market-wide evaluation, avoiding per-stock rate limits.
    """
    prompt = f"""
    You are an elite high-frequency quantitative trading Veto Agent.
    Review the following market data snapshot for our entire universe of stocks.
    Analyze momentum, volume anomalies, and technical strength across the board.
    
    Return a JSON array containing ONLY the stocks you explicitly approve for an immediate BUY action. 
    If none meet your strict thresholds, return an empty array [].

    Market Universe Data:
    {json.dumps(master_ticker_list, separators=(',', ':'))}

    Required JSON output format:
    [
      {{"ticker": "XYZ", "action": "BUY", "approve": true, "reasoning": "High relative volume breakout."}}
    ]
    """
    
    try:
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config={
                "response_mime_type": "application/json"
            }
        )
        return json.loads(response.text)
    except Exception as e:
        print(f"[Decide Engine] Error during full market sweep: {e}")
        return []
