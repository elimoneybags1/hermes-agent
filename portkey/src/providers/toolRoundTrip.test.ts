import { CohereChatCompleteConfig } from './cohere/chatComplete';
import { GoogleChatCompleteConfig, GoogleChatCompleteResponseTransform, GoogleChatCompleteStreamChunkTransform } from './google/chatComplete';

const messages = [
  { role: 'assistant', content: '', tool_calls: [{ id: 'call1', type: 'function', function: { name: 'read_probe', arguments: '{"key":"health"}', thought_signature: 'signed-state' } }] },
  { role: 'tool', tool_call_id: 'call1', content: 'probe-value' },
];

test('Cohere retains tool call and result correlation', () => {
  const config = CohereChatCompleteConfig.messages as any;
  const converted = config.transform({ messages });
  expect(converted[0].tool_calls).toEqual(messages[0].tool_calls);
  expect(converted[1].tool_call_id).toBe('call1');
  expect(converted[1].content).toBe('probe-value');
});

test('Google returns tool results as user function responses with the original name', () => {
  const config = (GoogleChatCompleteConfig.messages as any[])[0];
  const converted = config.transform({ messages });
  expect(converted[0].parts[0].thoughtSignature).toBe('signed-state');
  expect(converted[1].role).toBe('user');
  expect(converted[1].parts[0].functionResponse).toEqual({ name: 'read_probe', response: { output: 'probe-value' } });
});


test('Google accepts replay metadata after a proxy normalizes the function object', () => {
  const config = (GoogleChatCompleteConfig.messages as any[])[0];
  const normalized = JSON.parse(JSON.stringify(messages));
  delete normalized[0].tool_calls[0].function.thought_signature;
  normalized[0].tool_calls[0].extra_content = { google: { thought_signature: 'signed-state' } };
  expect(config.transform({ messages: normalized })[0].parts[0].thoughtSignature).toBe('signed-state');
});


test('Google emits replay metadata on both full and streaming tool calls', () => {
  const response: any = {
    candidates: [{ content: { role: 'model', parts: [{ functionCall: { name: 'read_probe', args: { key: 'health' } }, thoughtSignature: 'signed-state' }] }, finishReason: 'STOP' }],
    usageMetadata: { promptTokenCount: 10, candidatesTokenCount: 2, thoughtsTokenCount: 3, totalTokenCount: 15 },
  };
  const full: any = GoogleChatCompleteResponseTransform(response, 200, new Headers(), false);
  expect(full.choices[0].message.tool_calls[0].extra_content.google.thought_signature).toBe('signed-state');
  expect(full.usage.completion_tokens).toBe(5);
  expect(full.usage.total_tokens).toBe(15);
  const streamed = GoogleChatCompleteStreamChunkTransform(JSON.stringify(response), 'test', {}, false);
  const chunk = JSON.parse(streamed.split('data: ')[1].split('\n')[0]);
  expect(chunk.choices[0].delta.tool_calls[0].extra_content.google.thought_signature).toBe('signed-state');
  expect(chunk.usage.completion_tokens).toBe(5);
});
