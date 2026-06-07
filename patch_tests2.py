with open('tests/test_unified_bot.py', 'r') as f:
    lines = f.readlines()

with open('tests/test_unified_bot.py', 'w') as f:
    for line in lines:
        if line == "        def test_btc_hedge_throttle_key_hashes_message(self):\n":
            f.write("    def test_btc_hedge_throttle_key_hashes_message(self):\n")
        else:
            f.write(line)
