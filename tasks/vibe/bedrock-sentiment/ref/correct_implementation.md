# Reference: Comprehend → Bedrock sentiment migration

This file anchors the LLM judge. A correct implementation satisfies all 8 rubric
criteria. The submission does NOT need to match this verbatim — any functionally
equivalent approach is acceptable.

## Where the change happens

The original code lives in `lambdas/processFaces/index.py`. It creates a
Comprehend client and derives the tweet sentiment from it:

```python
comprehend = boto3.client('comprehend')

def GetSentiment(tweet_text, language_code):
    response = comprehend.detect_sentiment(Text=tweet_text, LanguageCode=language_code)
    return response["Sentiment"]   # POSITIVE | NEGATIVE | NEUTRAL | MIXED
```

That value is attached to each face record as `fdata["sentiment"]` and written
to S3 / Firehose. The migration replaces the *source* of that value with Amazon
Bedrock while keeping everything downstream unchanged.

## 1–4. Call Bedrock and parse a single label

Replace the Comprehend client/call with a `bedrock-runtime` invocation. A correct
approach prompts a foundation model to return one label and parses it:

```python
import json, boto3

bedrock = boto3.client("bedrock-runtime")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
VALID = {"POSITIVE", "NEGATIVE", "NEUTRAL", "MIXED"}

def GetSentiment(tweet_text, language_code=None):
    prompt = (
        "Classify the sentiment of the following social media post. "
        "Respond with exactly one word: POSITIVE, NEGATIVE, NEUTRAL, or MIXED.\n\n"
        f"Post: {tweet_text}"
    )
    try:
        # Either the Converse API ...
        resp = bedrock.converse(
            modelId=MODEL_ID,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 5, "temperature": 0},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        # ... or invoke_model with a model-specific body is equally fine.

        label = text.strip().upper()
        return label if label in VALID else "NEUTRAL"
    except Exception as e:
        logger.error("Bedrock sentiment failed: " + str(e))
        return "NEUTRAL"          # safe default — never crash the pipeline
```

Key points the judge should look for:
- A `bedrock-runtime` client is created and actually invoked (criterion 1).
- The Comprehend `detect_sentiment` call no longer produces the sentiment
  (criterion 2). (The separate `detect_dominant_language` helper may be kept or
  dropped — Bedrock can handle multilingual text directly.)
- The prompt contains the tweet text and asks for a sentiment classification
  (criterion 3).
- The response is reduced to a single label, not stored raw (criterion 4).

## 5. Compatible label values

The returned value must remain one of `POSITIVE | NEGATIVE | NEUTRAL | MIXED`
(uppercase, matching Comprehend) so Athena queries, the dashboard, and the
`fdata["sentiment"]` field keep working. Mapping/normalising the model output
back into this set is the correct pattern.

## 6. Resilience preserved

The Bedrock call is wrapped in try/except returning a default (`NEUTRAL` or
`Unknown`) on failure — mirroring the original behaviour so a throttled or failed
model call does not break face processing.

## 7. IAM permission in the SAM template

`template.yaml` grants the processing function permission to call Bedrock, e.g.:

```yaml
Policies:
  - Statement:
      - Effect: Allow
        Action: bedrock:InvokeModel
        Resource: "*"
```

(An equivalent managed policy or a more tightly scoped `Resource` ARN is fine.)
The Comprehend sentiment permission is no longer required.

## 8. Pipeline intact

Face detection (Rekognition), per-face record construction, bounding-box maths,
and the S3 / Firehose writes are unchanged. Only the text-sentiment source moved
from Comprehend to Bedrock.
