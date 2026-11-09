# Telephony providers for Pakistan (Option A: licensed SIP trunk + call forwarding)

> **Superseded for the current personal deployment (2026-11-07).** The project is a personal portfolio system using
> your own Redmi 9C and Ufone SIM through the Bluetooth HFP gateway (`docs/telephony-redmi9c-gateway.md`). No paid
> telephony is used. This research is kept unchanged for a possible future commercial version, which would need
> a fresh legal and architectural review.

Research date: 2026-11-07. **Nothing has been purchased.** Prices below are what providers *publish or
state on their websites*; none are quotes, and several pages are known to be templated or stale (noted).
Every figure must be confirmed in writing before any decision.

## How BYON works with Option A

```
Customer dials the business's existing number (e.g. 0300-xxxxxxx or 021-xxxxxxx)
   -> business's carrier forwards the call (conditional "no answer/busy" or unconditional)
   -> to a Pakistani DID on our SIP trunk (e.g. 021-xxxxxxx)
   -> trunk delivers SIP + RTP to our SIP/media host (public VPS, not Colab)
   -> voice engine (AI) handles the call; n8n identifies the business from the dialled DID
      and the forwarded-from (original called) number in the SIP headers
```

- **The business keeps its number.** The forwarding leg is billed to the business by its own carrier
  at its tariff. Standard PTA-listed outgoing rates are about Rs 2.7–3.5/min without packages
  ([Pakera, citing PTA list of 2026-09-30](https://pakera.pk/call-sms-data-rates-without-package-pakistan/)).
  Forwarding-specific rates were **not found** and must be checked per operator.
- **To verify with providers:** whether the original called number survives forwarding (SIP `Diversion` or
  `History-Info` headers). Without it, one DID per business is needed to identify the tenant; with it, one DID can serve many businesses.
- **Outbound calls** leave from our trunk's DID, so the caller sees *our* number, not the business's,
  unless the provider allows presenting the business's number with proof of ownership. Spoofing a caller ID
  is illegal (see below).
- **Mobile-number BYON:** mobile numbers are portable between operators, but landline/UAN/0800 numbers are
  not ([meni.ge guide, updated 2026-10-04](https://meni.ge/en/docs/sip-trunks-pakistan)). Call forwarding avoids porting entirely.

## Provider landscape

| Provider | Type | Numbers | Trunk for our own PBX | Published pricing | Notes |
|---|---|---|---|---|---|
| **PTCL** | PTA licensee (incumbent) | Landline, UAN 111, 0800 | Yes ("SIP TRUNK") | None. Corporate quote (1260, care@ptcl.net.pk) | National coverage. Parameters come from the contract only. |
| **Nayatel** | PTA licensee | Landline (mostly 051) | Yes | None in text (per-channel calculator) | Islamabad/Rawalpindi only. |
| **Wateen** | PTA licensee | Landline, UAN, 0800, DID/DOD | Yes | None. Corporate sales ([wateen.com/voice](https://wateen.com/voice/)) | Strong for multi-site / one UAN. |
| **Multinet** | PTA licensee | Landline, UAN, 0800 | Yes ("SIP Trunking") | None | Claims 130+ cities. |
| **Jazz Business** | Mobile operator | Mobile, UAN, 0800 | IP-PBX integration | None | Mobile caller ID. |
| **DIDHub** | Foreign aggregator (local partner) | Karachi 21, plus 42/51/41 | Yes, 5–200 channels | DID $10/mo; outbound $0.05/min landline, $0.10/min mobile; 25 ch $95/mo, 50 ch $180/mo; inbound per-minute charge **not stated** ([didhub.io](https://didhub.io/sip-trunks/pakistan)) | Lists "inbound AI voice agents" as an approved use case and **"cold outbound" as routinely rejected**. Ingress Dubai/Singapore. meni.ge warns the page looks templated: confirm by email. |
| **DIDWW** | Foreign aggregator | Landline, mobile, toll-free | Yes (two-way) | Not captured | Caller ID guaranteed per meni.ge. |
| **Telnyx** | Foreign CPaaS | Local numbers | Yes | From $42.50/mo per number (per meni.ge) | KYC 10–15 business days. |
| **Twilio** | Foreign CPaaS | Pakistani numbers **not confirmed** | Termination only confirmed | Termination $0.151/min fixed, $0.176/min mobile ([twilio.com](https://www.twilio.com/en-us/sip-trunking/pricing/pk)) | International route into Pakistan: expensive, and local caller ID is doubtful. |
| AVOXI, My Country Mobile, PBX.IM, Hivoox | Resellers | Various | Varies | Not verified | PBX.IM's page is flagged by meni.ge as copied from another country. **Do not rely on it.** |

**Initial shortlist for quotes:** PTCL, Wateen, and Multinet (national licensees, local caller ID, no foreign
hop), plus DIDHub or DIDWW as a fast foreign-aggregator comparison.

## Regulatory constraints (to be confirmed by a Pakistani telecom lawyer and/or PTA)

- Only PTA licensees may sell telephony. International voice must route through licensed LDI operators.
  Traffic that bypasses them is "grey traffic" and is enforced against
  ([PTA IP whitelisting regulations 2019](https://www.pta.gov.pk/assets/media/ip_white_listing_regulations_21022020.pdf)).
- **IP whitelisting:** the IP of our SIP/media host must be whitelisted through our licensee (PKR 500 per address
  per meni.ge). A host **outside Pakistan may not be accepted.** Ask every provider before signing whether a
  foreign VPS is acceptable or whether the SIP host must be in Pakistan.
- **Licensing for us:** PTA has at times stopped accepting new Voice/Data CVAS applications
  ([PTA notice](https://www.pta.gov.pk/category/public-notice-for-cvas-licenses-864741199-2023-06-01)), yet its Jan-2026
  licensee list still shows active Voice CVAS holders
  ([list](https://www.pta.gov.pk/assets/media/2026-01-19-List-of-CVAS-Licensees-in-Pakistan-dated-15-01-26.pdf)).
  Open question: does a SaaS that answers businesses' calls over a licensee's trunk need its own licence or PSEB
  registration, or is the licensee's service sufficient?
- **Caller ID spoofing** is an offence under the 2016 cybercrime law (per meni.ge). Outbound calls present our DID
  or a number the business has proven it owns.
- **Recording consent:** meni.ge reports an all-party-consent requirement and cites a March 2026 Supreme Court
  ruling. **Unverified.** The platform already plays a recording notice in the greeting and must stop recording on refusal.
- **Outbound calling:** aggregators reject cold outbound. Our outbound scheduler must only call customers with
  recorded consent or an existing transaction (order confirmation, booking reminder), and must honour opt-outs.

## Cost model (fill in with real quotes)

Per business per month = DID rental + channels + inbound minutes × inbound rate + outbound minutes × outbound
rate + (paid by the business to its own carrier) forwarded minutes × forwarding rate + our SIP/media VPS share.

Example using only DIDHub's published figures: 1 DID ($10) + 5 included channels + 500 outbound mobile minutes
($50) = **$60/month before inbound charges**, which are not published. This is illustrative, not a quote.

## Questions for every provider (RFQ checklist)

1. Can our SIP host be a VPS outside Pakistan, or must it be in Pakistan? Which IPs need PTA whitelisting, and who files the whitelisting?
2. Inbound per-minute rate, outbound landline/mobile rates, billing increment, setup fee, monthly fee per DID and per channel.
3. How many concurrent channels per trunk, and how fast can more be added?
4. Registration-based or IP-authenticated trunk? Codecs (G.711 A-law/µ-law required)? RFC 4733 DTMF? TLS/SRTP?
5. **Are `Diversion`/`History-Info` headers delivered on calls forwarded from Jazz/Zong/Telenor/Ufone/PTCL numbers?** (Decides one DID per business vs shared DIDs.)
6. Outbound caller ID policy: can we present a business's own number after proof of ownership?
7. Is AI-automated outbound to consenting customers permitted? Is any registration needed?
8. KYC documents, contract minimum term, activation time.
9. Do they provide a test DID or trial for SIP/RTP interop testing?

## Option B (kept as a future alternative)

A hardware GSM-to-SIP gateway gives line-level audio from ordinary SIMs, but it carries the same PTA questions (and
device type approval). Revisit only if no licensee will serve this use case.
