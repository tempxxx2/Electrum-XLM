# Setup masternode with separate owner, operator

## Operator wallet
Create DIP3 masternode on operator wallet with wizard.
"I am owner" checkbox should be unset.

<p><image src="operator/p1.png" width="800" /></p>

Select service params. BLS public key sould be copied and sent to the
owner of the masternode together with service params. BLS private key
should be saved to dash.conf of masternode Dash Core node,
with subsequent restart of the node.

<p><image src="operator/p2.png" width="800" />
   <image src="operator/p3.png" width="800" /></p>

Save new masternode data with preferred alias.

<p><image src="operator/p4.png" width="800" />
   <image src="operator/p5.png" width="800" /></p>

## Owner wallet
Create DIP3 masternode on operator wallet with wizard.
"I am operator" checkbox should be unset.

<p><image src="owner/p1.png" width="800" />
   <image src="owner/p2.png" width="800" /></p>

Set service params sent from operator, select Owner/Voting/Payout addresses.

<p><image src="owner/p3.png" width="800" />
   <image src="owner/p4.png" width="800" /></p>

Set BLS public key sent from operator, set operator reward in percents.

<p><image src="owner/p5.png" width="800" /></p>

Save new masternode data with preferred alias, send ProRegTx Transaction
with 1000 Dash output for collateral amount.

<p><image src="owner/p6.png" width="800" />
   <image src="owner/p7.png" width="800" />
   <image src="owner/p8.png" width="800" /></p>

State of saved masternode display changes after ProRegTx is confirmed,
and additional operations can be done on it (Update Registrar).

<p><image src="owner/p9.png" width="800" /></p>

## Operator wallet
State of saved masternode display changes after ProRegTx is confirmed,
and additional operations can be done on it (Update Service, Revoke Operator).
<p><image src="operator/p6.png" width="800" /></p>
