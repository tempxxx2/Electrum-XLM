# Setup masternode with combined owner and operator

Create a DIP3 masternode on a standard P2PKH wallet with the wizard.

<p><image src="op_own/p1.png" width="800" />
   <image src="op_own/p2.png" width="800" /></p>

Select service params and Owner/Voting/Payout addresses.
The BLS private key should be saved to dash.conf of the masternode Dash Core node,
with subsequent restart of the node.

<p><image src="op_own/p3.png" width="800" />
   <image src="op_own/p4.png" width="800" />
   <image src="op_own/p5.png" width="800" /></p>

Save the new masternode data with a preferred alias, send the ProRegTx Transaction
with 1000 Dash output for collateral amount.

<p><image src="op_own/p6.png" width="800" />
   <image src="op_own/p7.png" width="800" />
   <image src="op_own/p8.png" width="800" /></p>

The state of the saved masternode displays changes after the ProRegTx is confirmed,
and additional operations can be done on it (Update Registrar or Service).

<p><image src="op_own/p9.png" width="800" /></p>
