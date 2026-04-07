import { useRef, useCallback } from 'react';

/**
 * Queue for turbo-mode fast scanning.
 * Enqueues barcodes and processes them sequentially so rapid scans
 * don't race or get dropped while an API call is in-flight.
 */
export default function useScanQueue(processFn) {
  const queue = useRef([]);
  const processing = useRef(false);

  const processQueue = useCallback(async () => {
    if (processing.current) return;
    processing.current = true;

    while (queue.current.length > 0) {
      const barcode = queue.current.shift();
      try {
        await processFn(barcode);
      } catch {
        // errors handled inside processFn
      }
    }

    processing.current = false;
  }, [processFn]);

  const enqueue = useCallback((barcode) => {
    queue.current.push(barcode);
    processQueue();
  }, [processQueue]);

  return enqueue;
}
