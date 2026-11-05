-- Chapter 5 runtime metadata; business tools and existing tables remain shared.
SET NAMES utf8mb4;
CREATE TABLE workflow_turns (
  turn_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  conversation_id BIGINT UNSIGNED NOT NULL,
  user_message_id BIGINT UNSIGNED NOT NULL,
  final_message_id BIGINT UNSIGNED NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'active',
  payload JSON NOT NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (turn_id),
  KEY ix_workflow_turns_conversation_id (conversation_id),
  UNIQUE KEY user_message_id (user_message_id),
  UNIQUE KEY final_message_id (final_message_id),
  FOREIGN KEY (conversation_id) REFERENCES conversations(id),
  FOREIGN KEY (user_message_id) REFERENCES messages(id),
  FOREIGN KEY (final_message_id) REFERENCES messages(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
CREATE TABLE workflow_actions (
  action_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  turn_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  origin_assistant_message_id BIGINT UNSIGNED NOT NULL,
  kind VARCHAR(16) NOT NULL,
  status VARCHAR(16) NOT NULL DEFAULT 'offered',
  request_hash VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  tool_call_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  request_message_id BIGINT UNSIGNED NULL,
  result JSON NULL,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (action_id),
  UNIQUE KEY uq_workflow_turn_kind (turn_id,kind),
  UNIQUE KEY tool_call_id (tool_call_id),
  FOREIGN KEY (turn_id) REFERENCES workflow_turns(turn_id),
  FOREIGN KEY (origin_assistant_message_id) REFERENCES messages(id),
  FOREIGN KEY (request_message_id) REFERENCES messages(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
