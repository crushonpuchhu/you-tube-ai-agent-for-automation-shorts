# Privacy Policy — YouTube Shorts Automation Agent

**Last updated:** 22 October 2026

This privacy policy applies to the "YouTube Shorts Automation Agent" (the
"application"), a personal automation tool whose source code is available at
<https://github.com/crushonpuchhu/you-tube-ai-agent-for-automation-shorts>.

## Who operates this application

The application is operated by a single individual (the "developer"),
contactable at **mittalkumargupta@gmail.com**. It is not offered as a public
service. The only person who authorises it is the developer, who is also the
sole end user.

## What data the application processes

The application runs entirely on infrastructure controlled by the developer.
It does not operate any website, account system, or analytics backend that
members of the public interact with.

### YouTube account data
When the developer authorises the application with Google OAuth, it receives
and uses these scopes:

- `https://www.googleapis.com/auth/youtube.upload` — to upload Shorts to the
  developer's own YouTube channel.
- `https://www.googleapis.com/auth/youtube.readonly` — to verify that an
  upload succeeded and to read the developer's own video statistics.
- `https://www.googleapis.com/auth/youtube-analytics.readonly` — to build the
  developer's own weekly performance report and scheduling recommendations.

No data belonging to other YouTube users, viewers, or commenters is collected,
stored, or processed.

### Google Gemini
Video files, titles, descriptions and tags are sent to the Google Gemini API
for analysis so the application can write metadata and suggest captions.
This is governed by Google's own terms and privacy policy for that service.

### Telegram
The application receives video files and text messages sent by the developer
to its Telegram bot, and sends notifications back to the same chat. Telegram
messages are processed only as needed to run the publishing workflow. No
messages from other chats are read.

### Files stored locally
The application stores the following on the developer's own machine or server:

- Downloaded video files.
- The Google OAuth token (used to refresh access to the developer's own
  channel).
- A SQLite database of published and scheduled Shorts and their statistics.

## What the application does not do

- It does not sell, share, or transfer personal data to third parties.
- It does not use the data for advertising, profiling, or marketing.
- It does not track members of the public.
- It does not expose credentials or tokens in logs or notifications.

## Data retention

Video files, tokens and the SQLite database remain on the developer's
infrastructure until the developer deletes them. Tokens are stored with
restricted file permissions and are never committed to the public source
repository.

## Third-party services

The application uses the following third-party services, each governed by its
own privacy policy:

- [Google Cloud / Google OAuth](https://policies.google.com/privacy)
- [Google Gemini](https://policies.google.com/privacy)
- [YouTube](https://policies.google.com/privacy)
- [Telegram](https://telegram.org/privacy)

## Changes to this policy

Any change to this policy is published in the source repository at the URL
above together with the code change that caused it.

## Contact

Questions about this privacy policy: **mittalkumargupta@gmail.com**.
