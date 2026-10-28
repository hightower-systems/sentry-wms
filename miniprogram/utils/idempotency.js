function createIdempotencyKey(prefix) {
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

module.exports = {
  createIdempotencyKey,
};
