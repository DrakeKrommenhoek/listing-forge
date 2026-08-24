# Dedicated buyer inbox — setup package

Nothing in this document has been created or purchased. It is a checklist for
you to execute. **Do not use a personal email address for selling.** Buyer
inquiries attract spam, scam attempts, and eventually a stranger who has your
address; all of that should land somewhere disposable and separable.

---

## 1. Inbox naming options

Pick one. In order of preference:

| Option | Example | Why |
|---|---|---|
| Neutral + non-identifying (recommended) | `thecollection.sales@gmail.com` | Reveals nothing about who or where you are. Survives a rename of the catalogue. |
| Catalogue-branded | `hello@<catalogue-domain>` | Looks the most professional; requires you to own a domain. |
| Purpose-only | `estatesale.pnw@gmail.com` | Clear to buyers, still non-identifying. Region hint helps local buyers trust it. |
| Family-name based | `yourlastnamesale@gmail.com` | **Avoid.** Ties the listings to a real family name and, by extension, to a searchable address. |

Rules regardless of choice:

- No street name, house number, or full personal name.
- No birth year or other credential-stuffing hints.
- Short enough to read out over the phone.

## 2. Setup instructions

1. Create the account in a private browser window while signed out of every
   other account, so it is not silently linked to a personal profile.
2. Set the display name to the catalogue name, not a person's name.
3. Add a recovery phone and recovery email that **you** control — not Dad's, if
   he is not the one who will run the inbox day to day.
4. Set a signature that contains the catalogue URL and the phrase
   "Item reference:" so every reply carries an item ID.
5. Turn off any "smart reply" or auto-response feature. An automated
   "Yes, still available!" on an item that just sold creates a wasted trip.

## 3. Security instructions

- A unique password from a password manager. Never reuse a personal password.
- **Two-factor authentication on from day one** — see the checklist below.
- Never click links in buyer emails. Payment "confirmations", shipping-label
  requests, and "verification code" messages are the three standard scams.
- Never send or accept a verification code. A buyer asking you to read back a
  code you just received is stealing the account, not verifying you.
- Never publish the house address anywhere. Share it only after a pickup time
  is agreed, and only in a direct message.
- Prefer cash or an instant payment app **on collection**. Do not accept a
  cheque, a "cashier's cheque", an overpayment, or a request to ship to a
  freight forwarder.

### Two-factor authentication checklist

- [ ] 2FA enabled on the inbox account
- [ ] Method is an authenticator app or a passkey — **not SMS** where avoidable
- [ ] Backup codes downloaded and stored in the password manager
- [ ] Backup codes also stored somewhere offline (printed, in a drawer)
- [ ] Recovery email set and itself protected by 2FA
- [ ] Recovery phone set to a number you will still control in six months
- [ ] A second trusted person knows how to reach the recovery method
- [ ] 2FA verified by signing out and back in once, before the first listing

## 4. Shared access options

| Approach | How | Trade-off |
|---|---|---|
| Delegation (recommended for Gmail) | Settings → Accounts → Grant access to your account | Second person reads and replies without the password; no credential sharing; their replies are attributed. |
| Shared password in a password manager vault | 1Password / Bitwarden shared item | Simple, but you cannot tell who did what, and revoking means a password change. |
| Forwarding to a personal inbox | Filter → forward | Fine for *reading*; replies then come from a personal address, which defeats the purpose. Use for alerts only. |
| Google Group / shared mailbox | Group with both addresses as members | Cleanest for three or more people; more setup. |

Whatever you pick, write down who has access and revisit it the day the sale
ends.

### Recovery-access plan

1. The password lives in a password manager vault shared with one trusted person.
2. Backup 2FA codes live in the same vault **and** on paper.
3. The recovery email is an account that person can also reach.
4. If the primary holder is unreachable for 48 hours, the second person can
   recover the account without contacting anyone else.
5. When the sale is finished: revoke delegated access, remove the shared vault
   item, and either close the account or leave it dormant with 2FA intact.

## 5. Labels

Create these, in this order. They map exactly to the statuses in the inventory.

- `New Inquiry`
- `Responded`
- `Negotiating`
- `Pickup Scheduled`
- `Shipping`
- `Sold`
- `Spam or Scam`
- `Needs Follow-Up`

## 6. Filters

| Filter | Condition | Action |
|---|---|---|
| Website form | From your D.R.A.K.E. sending address, or subject contains `[DK-` | Apply `New Inquiry`, star |
| Marketplace notifications | From `@facebook.com`, `@ebay.com`, `@reverb.com`, `@poshmark.com` | Apply `New Inquiry`, skip inbox if noisy |
| Obvious scam language | Contains "shipping agent", "cashier's check", "my mover will collect", "PayPal invoice attached" | Apply `Spam or Scam`, skip inbox, never auto-delete (you may need the evidence) |
| Follow-up sweep | Older than 3 days and label is `Responded` | Apply `Needs Follow-Up` (run manually or via a saved search) |
| Pickup confirmations | Subject contains `PICKUP CONFIRMED` | Apply `Pickup Scheduled` |

Set up a saved search for `label:New Inquiry is:unread` and check it twice a
day. Response speed is the single biggest driver of local-marketplace sales.

## 7. Item-ID subject formatting

Every thread should carry the item ID so the inbox and the spreadsheet stay
joinable.

```
[DK-202608-014] Oak dining table — availability
[DK-202608-014] Pickup confirmed — Saturday 10:00
[DK-202608-014 + DK-202608-021] Bundle enquiry
```

Rules:

- The ID goes first, in square brackets. Filters and searches depend on it.
- Never change the ID in a reply — it breaks the thread's link to the row.
- Bundles list every ID, separated by ` + `.
- The website contact form and the D.R.A.K.E. inquiry endpoint already format
  subjects this way.

## 8. Contact-form routing

```
Website form  ──POST──▶  /estate/inquiry  ──▶  estate_inquiries table
                                           └─▶  item.inquiry_count += 1
                                           └─▶  email to the selling inbox
                                                subject: [ITEM_ID] Website enquiry
```

The `/estate/inquiry` endpoint is already implemented and stores the inquiry.
**Email delivery is not yet wired** — see "Known gaps" in `PROJECT_STATE.md`.
Until it is, check the review interface or query `estate_inquiries` directly.

---

## 9. Message templates

Fill the `<angle brackets>`. Keep them short — long replies read as desperate
and invite negotiation.

### Confirming availability

> Subject: [<ITEM_ID>] Still available
>
> Hi <name>,
>
> Yes, the <item> is still available. It's <price>, or <pickup price> if you
> collect it yourself.
>
> It's also listed on a couple of other sites, so the first confirmed pickup
> takes it. When would suit you?
>
> <catalogue URL>

### Answering condition questions

> Subject: [<ITEM_ID>] Condition
>
> Hi <name>,
>
> Happy to go through it. Condition is <condition>. To be upfront: <defects>.
>
> Dimensions are <dimensions>, and it comes with <accessories>. All the photos
> are of the actual item — I can take more of any specific area, just say which.

### Responding to a lower offer

> Subject: [<ITEM_ID>] Your offer
>
> Hi <name>,
>
> Thanks for the offer. I can't do <their offer> on this one — the price is
> based on what comparable pieces have actually sold for recently.
>
> I can do <counter> if you can collect this week. If you're also interested in
> anything else from the catalogue I can do better on a bundle.

### Confirming a bundle discount

> Subject: [<ID 1> + <ID 2> + <ID 3>] Bundle
>
> Hi <name>,
>
> For all three — <item 1>, <item 2>, and <item 3> — I can do <bundle price>
> collected, instead of <sum> separately.
>
> That holds until <date>. If it works, tell me a pickup window and I'll send
> the address.

### Scheduling pickup

> Subject: [<ITEM_ID>] PICKUP CONFIRMED — <day> <time>
>
> Hi <name>,
>
> <day> at <time> works. The address is <address> — please don't share it on.
>
> A few practical notes:
> - Bring <vehicle>, and a second person: it's a two-person lift.
> - It's <disassembly note>.
> - Payment is <cash / app> on collection.
>
> If anything changes, text me on <number> rather than emailing.

### Confirming payment

> Subject: [<ITEM_ID>] Received — thank you
>
> Hi <name>,
>
> Payment received and the <item> is yours. Thanks for making that easy.
>
> If you're after anything else, the rest of the catalogue is at <URL> and it
> changes weekly.

### Declining a suspicious transaction

> Subject: [<ITEM_ID>] Not proceeding
>
> Hi,
>
> Thanks for your interest, but I only sell in person, for cash or an instant
> payment app, on collection. I don't ship, don't take cheques or overpayments,
> and don't arrange third-party couriers.
>
> If that doesn't suit, no problem — best of luck with your search.

*(Do not argue, do not explain further, do not send photos of anything with a
serial number. Label the thread `Spam or Scam` and move on.)*

### Following up with an interested buyer

> Subject: [<ITEM_ID>] Still interested?
>
> Hi <name>,
>
> Just checking in on the <item> — are you still interested? It's had a bit of
> attention and I'd rather give you first refusal before it goes.
>
> Happy to hold it for 24 hours if you can tell me a pickup time.

---

## 10. Address safety

- The address appears nowhere: not in listings, not on the website, not in the
  first reply, not in photo metadata (the site generator strips EXIF, but only
  from the derivatives it creates — never upload an original phone photo
  directly to a marketplace).
- Share the address only after a specific pickup time is agreed.
- For high-value items, meet in a public place where practical, or have a
  second person at home during the pickup window.
- Never let a buyer wander the house. Bring the item to a garage, porch, or
  driveway before they arrive.
