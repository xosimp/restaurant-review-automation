# Meta App Review: Cavnar AI (app 1926741024712844)

Status: the June 12, 2026 submission was rejected for every permission except
`public_profile`. Each rejection said the same thing: **"Screencast Not
Aligned with Use Case Details."** Meta found the use case **allowed**; the
video didn't show the whole flow end to end. Nothing about the product has to
change. The resubmission needs a better recording and descriptions that point
into it.

The app connects through **Facebook Login for Business** with a **user access
token** (configuration "Cavnar AI Connect", `META_LOGIN_CONFIG_ID`). It does
not use a system-user token or server-to-server access, so the Meta login
must appear in the video.

## The eight permissions, and where the code uses each

| Permission | What Cavnar AI does with it | Code |
|---|---|---|
| `pages_show_list` | Lists the Pages the person manages, so they can choose which Page is this restaurant | `social_routes._read_pages` (`me/accounts`) |
| `business_management` | Most restaurant Pages are owned by a business portfolio; without it, those Pages don't appear in the list | same call |
| `pages_read_engagement` | Reads the Page's name and Page token, and a post's reactions, comments and shares | `_read_pages`, `_fb_post_metrics` |
| `pages_manage_posts` | Publishes the post the owner approved to their Page (`/{page}/photos` or `/{page}/feed`) | `post_to_facebook` |
| `read_insights` | Reads a published post's impressions and reach (`/{post}/insights`) for the owner's Marketing figures | `_fb_post_metrics` |
| `instagram_basic` | Reads the Instagram account linked to the Page (id, username), and a post's like and comment counts | `_read_pages`, `_ig_post_metrics` |
| `instagram_content_publish` | Publishes the approved post to that Instagram account (`/{ig}/media` then `/media_publish`) | `post_to_instagram` |
| `instagram_manage_insights` | Reads a published Instagram post's reach, likes, comments and shares (`/{media}/insights`) | `_ig_post_metrics` |

## One recording, ~3–4 minutes (attach the same file to every permission)

Record in English, in a desktop browser at 1080p, with a caption on screen at
every step that says what is happening and what each button does. Captions
can be added in iMovie or Screen Studio. Start from a browser that is
**signed out of Facebook**. Use a Facebook account that manages a test Page
with a linked Instagram professional account.

| Time | Screen | Caption |
|---|---|---|
| 0:00 | dashboard.cavnar.ai sign-in → signed in | "Cavnar AI is a dashboard restaurant owners use to write and publish their marketing." |
| 0:15 | Account → Connections → **Connect Instagram & Facebook** | "The owner connects their restaurant's Facebook Page and Instagram account so Cavnar AI can publish posts they approve." |
| 0:25 | Facebook login popup: enter email and password, sign in | "Meta login: the owner signs in to Facebook." |
| 0:40 | "Continue as …", the Page and Instagram choice, the permissions screen. **Hold on it for 3 seconds**, then Continue / Save | "The owner chooses their Page and Instagram account and grants Cavnar AI these permissions." |
| 1:00 | Back in Cavnar AI: "Which Page is this restaurant?" → pick the Page (if more than one) → the Connections card shows the Page name and @instagram username | "pages_show_list and business_management: Cavnar AI lists the Pages this person manages, including Pages owned by a business portfolio. instagram_basic: it reads the linked Instagram account's username." |
| 1:20 | Marketing → Content: a drafted post with a photo → **Post to Facebook** → success | "pages_manage_posts: the owner reviewed this post; clicking Post to Facebook publishes it to their Page." |
| 1:40 | New tab: the Facebook Page, the post visible on it | "The post is live on the restaurant's Facebook Page." |
| 1:55 | Back → **Post to Instagram** → success | "instagram_content_publish: clicking Post to Instagram publishes the same post to the linked Instagram account." |
| 2:10 | New tab: instagram.com profile, the post visible | "The post is live on Instagram." |
| 2:25 | Marketing → Analytics → post performance for an earlier post: reach, likes, comments, shares | "read_insights, pages_read_engagement and instagram_manage_insights: Cavnar AI reads each published post's reach and engagement so the owner can see which posts worked." |
| 2:50 | Account → Connections → Disconnect (optional) | "The owner can disconnect at any time." |

For the last step, use a post that's at least a day old, so Meta has reach
figures for it. A brand-new post shows nothing yet.

## Descriptions (paste one into each permission's "How will your app use…" box)

Every description ends with the timestamp, so the reviewer can jump to it.

**pages_show_list**
Cavnar AI is a marketing dashboard for restaurant owners. When an owner clicks "Connect Instagram & Facebook" in Account → Connections and signs in with Facebook, Cavnar AI calls me/accounts to list the Pages they manage. They choose which Page is their restaurant ("Which Page is this restaurant?"), and Cavnar AI saves that Page so it can publish posts the owner approves to it. Shown in the screencast at 1:00.

**business_management**
Most restaurant Pages are owned by a business portfolio rather than a personal profile, and the restaurant's manager usually reaches the Page through that portfolio. business_management lets the Page list in Cavnar AI's connect step (me/accounts) include those business-owned Pages, so the owner can choose their restaurant's Page. Cavnar AI does not create, change or read anything else in the business portfolio. Shown in the screencast at 1:00.

**pages_read_engagement**
After the owner chooses their Page, Cavnar AI reads the Page's name and Page access token so it can publish to it, and shows the Page name on the Connections card. After a post is published, Cavnar AI reads that post's reactions, comments and shares so the owner sees how it did under Marketing → Analytics. Shown in the screencast at 1:00 and 2:25.

**pages_manage_posts**
Cavnar AI helps a restaurant owner write a post (text and photo). The owner reviews it and clicks "Post to Facebook"; only then does Cavnar AI publish it to the Page they connected. Nothing is ever posted without that click. Shown in the screencast at 1:20, with the published post on the Page at 1:40.

**read_insights**
For each post the owner published through Cavnar AI, Cavnar AI reads that post's impressions and reach (/{post-id}/insights). It shows them under Marketing → Analytics so the owner can see which posts reached the most people. Only posts published through Cavnar AI are read. Shown in the screencast at 2:25.

**instagram_basic**
When the owner connects their Facebook Page, Cavnar AI reads the Instagram professional account linked to that Page (its id and username) and shows the @username on the Connections card, so the owner can confirm the right account is connected. After a post is published it reads the post's like and comment counts. Shown in the screencast at 1:00 and 2:25.

**instagram_content_publish**
The owner reviews a post Cavnar AI helped write and clicks "Post to Instagram". Only then does Cavnar AI create the media container and publish it to the Instagram account linked to their Page. Nothing is posted without that click. Shown in the screencast at 1:55, with the published post on Instagram at 2:10.

**instagram_manage_insights**
For each Instagram post the owner published through Cavnar AI, Cavnar AI reads that post's reach, likes, comments and shares (/{media-id}/insights). It shows them under Marketing → Analytics so the owner can see which posts worked. Only posts published through Cavnar AI are read. Shown in the screencast at 2:25.

## Test instructions for the reviewer (the "Notes" box)

1. Go to https://dashboard.cavnar.ai and sign in with the test login below.
2. Open Account → Connections and click "Connect Instagram & Facebook". Sign in with a Facebook account that manages a Page linked to an Instagram professional account, and grant the permissions.
3. If you manage more than one Page, choose one on "Which Page is this restaurant?".
4. Open Marketing → Content, choose a post with a photo, and click "Post to Facebook", then "Post to Instagram".
5. Marketing → Analytics shows each published post's reach and engagement once Meta reports it, usually within a day.

The app uses Facebook Login for Business with a user access token, not a system-user token.

Test login: _(an owner login on a demo restaurant, made for the reviewer; never a client's account)_

## Before submitting

- The configuration "Cavnar AI Connect" carries all eight permissions.
- The review request lists all eight; `instagram_business_*` are not included.
- Business verification is complete (Review → Verification).
- Privacy policy and data-deletion URLs are set in App settings → Basic.
