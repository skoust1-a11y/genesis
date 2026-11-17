# Archived tier documentation

Source: /Users/skoust/Documents/GenesisArchives/genesis-original-20261114/README.md
SHA256: 0ed209c6c78725f1eb93676866bfc28c6e47881e2217a04a185c1f9f368ea755

## 🛡️ Quantitative Risk & Execution Framework

### 1. The 3-Tier Execution Framework
Genesis uses a dynamic 3-Tier lot and strike sizing architecture:

| Tier | Category | Strike Selection | Lots | Purpose |
| :--- | :--- | :--- | :---: | :--- |
| **Tier 1** | **Standard Momentum** | Strictly 1-OTM (50-100 pts away) | **3 Lots** (195 shares) | Captures high delta expansion during verified momentum breaks while minimizing theta decay. |
| **Tier 2** | **Super-Trend Expansion** | 2-OTM | **4 Lots** (260 shares) | High convex gamma upside during multi-timeframe aligned institutional trends. |
| **Tier 3** | **Scalp / Counter-Trend** | At-the-Money (ATM) | **2 Lots** (130 shares) | Fast delta-heavy absorption setups with tight spot invalidation stops. |

### 2. Institutional 50% Capital Safety Wall
To prevent give-back of trading profits, Genesis enforces the **Capital Safety Wall**:

$$\text{Safety Wall Reserve} = 0.50 \times \max(0, \text{Cumulative Net Realized Profits})$$
$$\text{Tradeable Risk Capital} = \text{Base Capital} + 0.50 \times \text{Cumulative Net Realized Profits}$$

- **50% of all accumulated profits are permanently locked into the capital floor.**
- Drawdowns are calculated strictly against the tradeable surplus. If the floor is touched, trading is automatically halted.

### 3. Exit Discipline & Dynamic R:R
- **Target 1 (+1.8R)**: Partial book 50% of position; instantly move stop loss to entry price (breakeven).
- **Target 2 (+2.5R)**: Book 25% of position; trail remaining runner using 5M swing structure pivots.
- **Target 3 (+3.5R to +4.5R)**: Unconditional 100% full exit; zero runner exposure carried beyond Target 3.
- **Auto-Flip Reversal**: If an opposing structural sweep forms while holding, position is closed immediately at market.

---
