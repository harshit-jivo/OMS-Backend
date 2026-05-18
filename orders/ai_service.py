def get_order_summary(data):
    """
    Generates an AI summary based on the order data provided.
    """
    party = data.get('party', 'Unknown Party')
    state = data.get('state', 'Unknown State')
    amount = data.get('amount', 0)
    
    # For now, returning a mock summary. You can integrate an actual AI SDK here later.
    summary = f"Order Analysis for {party} ({state}): The total order amount is ₹{amount}. "
    return summary