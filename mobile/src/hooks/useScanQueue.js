import { useRef, useCallback, useState } from 'react';

/**
 * Queue for turbo-mode fast scanning.
 * Enqueues barcodes and processes them sequentially so rapid scans
 * don't race or get dropped while an API call is in-flight.
 *
 * Returns [enqueue, isProcessing] — use isProcessing to disable
 * the scan input while an API call is in-flight.
 */
export default function useScanQueue(processFn) {
  const queue = useRef([]);
  const processingRef = useRef(false);
  const [isProcessing, setIsProcessing] = useState(false);

  const processQueue = useCallback(async () => {
    if (processingRef.current) return;
    processingRef.current = true;
    setIsProcessing(true);

    while (queue.current.length > 0) {
      const barcode = queue.current.shift();
      try {
        await processFn(barcode);
      } catch {
        // errors handled inside processFn
      }
    }

    processingRef.current = false;
    setIsProcessing(false);
  }, [processFn]);

  const enqueue = useCallback((barcode) => {
    queue.current.push(barcode);
    processQueue();
  }, [processQueue]);

  return [enqueue, isProcessing];
}
