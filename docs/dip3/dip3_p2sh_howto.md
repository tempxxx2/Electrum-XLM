# Setup masterode with P2SH collateral output stored on multisig wallet

To store collateral amount on multisig P2SH wallet collateral amount
must be created as output of ProRegTx transaction. Existing collateral
amount of 1000 Dash stored on multisig is not suited for ProRegTx.


There are two methods:

1. Create ProRegTx transaction on standard P2PKH wallet, and set 1000 Dash
   output of ProRegTx to P2SH multisig address of second wallet.

2. Create ProRegTx transaction on multisig P2SH wallet, and set Owner/Voting
   addresses to second standard wallet P2PKH adresses (these addresses must
   be of P2PKH type).

## Method 1. Create from standard wallet, store collateral on multisig wallet.

Create DIP3 masternode on standard P2PKH wallet with wizard.

<p><image src="dip3_p2sh/protx_p1.png" width="800" /></p>

Then select collateral as ProRegTx Output.

<p><image src="dip3_p2sh/protx_p2.png" width="800" /></p>

Select service params and Owner/Voting/Payout addresses, save BLS priv key,
finish wizard.

<p><image src="dip3_p2sh/protx_p3.png" width="800" />
   <image src="dip3_p2sh/protx_p4.png" width="800" />
   <image src="dip3_p2sh/protx_p5.png" width="800" /></p>

Send "Pay to" address of ProRegTx to P2SH address of your multisig wallet,
set "Amount" to 1000 Dash value of collateral.

<p><image src="dip3_p2sh/protx_p6.png" width="800" /></p>

## Method 2. Create from multisig wallet.

Create DIP3 masternode on multisig P2SH wallet with wizard.

<p><image src="dip3_p2sh/protx_p1.png" width="800" /></p>

Then select collateral as ProRegTx Output.

<p><image src="dip3_p2sh/protx_p2.png" width="800" /></p>

Select service params.

<p><image src="dip3_p2sh/protx_p3.png" width="800" /></p>

Select Owner/Voting addresses from your standard P2PKH wallet.
Payout addresses can be on P2SH or P2PKH wallet.

<p><image src="dip3_p2sh/protx_p4_p2sh.png" width="800" /></p>

Save BLS priv key, finish wizard.

<p><image src="dip3_p2sh/protx_p5.png" width="800" /></p>

Send "Pay to" address of ProRegTx to P2SH address of your multisig wallet,
set "Amount" to 1000 Dash value of collateral.

<p><image src="dip3_p2sh/protx_p6.png" width="800" /></p>
