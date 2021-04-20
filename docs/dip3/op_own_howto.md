# Setup masternode with combined owner and operator

Create DIP3 masternode on standard P2PKH wallet with wizard.

<p><image src="op_own/p1.png" width="800" />
   <image src="op_own/p2.png" width="800" /></p>

Select service params and Owner/Voting/Payout addresses.
BLS private key sould be saved to dash.conf of masternode Dash Core node,
with subsequent restart of the node.

<p><image src="op_own/p3.png" width="800" />
   <image src="op_own/p4.png" width="800" />
   <image src="op_own/p5.png" width="800" /></p>

Save new masternode data with preferred alias, send ProRegTx Transaction
with 1000 Dash output for collaterl amount.

<p><image src="op_own/p6.png" width="800" />
   <image src="op_own/p7.png" width="800" />
   <image src="op_own/p8.png" width="800" /></p>

State of saved masternode display changes after ProRegTx is confirmed,
and additional operations can be done on it (Update Registrar or Service).

<p><image src="op_own/p9.png" width="800" /></p>
