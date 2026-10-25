;;; SBCL-only versioned session. Only its owner thread touches the socket.

(in-package #:lisp-mcp)

(defconstant +genera-control-limit+ 16)

(define-condition genera-session-error (simple-error)
  ((kind :initarg :kind :reader genera-session-error-kind)
   (session :initarg :session :initform nil :reader genera-session-error-session)
   (response :initarg :response :initform nil :reader genera-session-error-response)))

(defun genera-session-error (kind message &optional response)
  (error 'genera-session-error :kind kind :response response
         :format-control "~A" :format-arguments (list message)))

(defstruct (genera-request (:constructor %make-genera-request))
  envelope payload execution-p (state :queued) response failure)

(defstruct (genera-session (:constructor %make-genera-session))
  host port token expected-world (io-timeout 5) owner socket hello
  (lock (sb-thread:make-mutex :name "Genera session"))
  (changed (sb-thread:make-waitqueue :name "Genera session"))
  (nonce (format nil "~A-~D" *genera-request-nonce*
                 (random (expt 2 64) (make-random-state t))))
  (counter 0) (state :connecting) (phase :connect) blocking stop failure cached-status
  (pending (make-hash-table :test 'equal)) queue execution (controls 0)
  ;; Incremental framing lets queued controls pass even during a partial reply.
  (prefix :start) (digits 0) (length 0) input (position 0))

(defun genera-session-checked (function)
  (handler-case (funcall function)
    (error (condition) (genera-session-error :protocol (princ-to-string condition)))))

(defmacro genera-with-io (session &body body)
  `(unwind-protect
       (progn
         (setf (genera-session-blocking ,session) t)
         (when (genera-session-stop ,session)
           (genera-session-error :closed "Genera host I/O closed"))
         ,@body)
     (setf (genera-session-blocking ,session) nil)))

(defun genera-session-fail (session condition)
  (sb-thread:with-mutex ((genera-session-lock session))
    (setf (slot-value condition 'session) session)
    (setf (genera-session-failure session) condition
          (genera-session-state session) (if (genera-session-stop session) :closed :failed)
          (genera-session-phase session) :failed
          (genera-session-queue session) nil)
    (maphash (lambda (id request)
               (declare (ignore id))
               (setf (genera-request-failure request) condition
                     (genera-request-state request)
                     (if (eq (genera-request-state request) :queued)
                         :not-submitted :unresolved)))
             (genera-session-pending session))
    (sb-thread:condition-broadcast (genera-session-changed session))))

(defun genera-session-check-response (request response)
  (unless (every (lambda (key) (equal (getf request key) (getf response key)))
                 '(:id :boot-id :service-id))
    (mcp-v1-error "Response identity does not match its request"))
  (case (getf request :op)
    (:eval (mcp-v1-check-eval-response request response))
    (:status
     (when (getf response :ok)
       (dolist (key '(:world :package :busy :features))
         (unless (mcp-v1-present-p response key) (mcp-v1-error "Incomplete status")))))
    (:request-status
     (when (getf response :ok)
       (unless (and (equal (getf request :target-id) (getf response :target-id))
                    (mcp-v1-present-p response :request-state)
                    (eq (not (null (getf response :result)))
                        (not (null (member (getf response :request-state)
                                           '(:completed :failed :interrupted))))))
         (mcp-v1-error "Incomplete or mismatched request status")))))
  response)

(defun genera-session-deliver (session text)
  (let ((response (genera-session-checked (lambda () (mcp-v1-decode text :response)))))
    (sb-thread:with-mutex ((genera-session-lock session))
      (let* ((id (getf response :id))
             (request (gethash id (genera-session-pending session))))
        (unless (and request (eq (genera-request-state request) :sent))
          (genera-session-error :protocol "Unmatched or repeated response ID"))
        (genera-session-checked
         (lambda () (genera-session-check-response (genera-request-envelope request) response)))
        (when (and (getf response :ok)
                   (eq (getf (genera-request-envelope request) :op) :status))
          (unless (equal (getf response :world) (getf (genera-session-hello session) :world))
            (genera-session-error :protocol "Status world changed without a new generation"))
          (setf (genera-session-cached-status session) response))
        ;; Validate everything before completing a waiter or releasing its slot.
        (setf (genera-request-response request) response
              (genera-request-state request) :completed)
        (remhash id (genera-session-pending session))
        (if (genera-request-execution-p request)
            (setf (genera-session-execution session) nil)
            (decf (genera-session-controls session)))
        (sb-thread:condition-broadcast (genera-session-changed session))))))

(defun genera-session-byte (session byte)
  (when (genera-session-input session)
    (setf (aref (genera-session-input session) (genera-session-position session)) byte)
    (when (= (incf (genera-session-position session)) (genera-session-length session))
      (genera-session-deliver session (map 'string #'code-char (genera-session-input session)))
      (setf (genera-session-input session) nil (genera-session-prefix session) :start
            (genera-session-digits session) 0 (genera-session-length session) 0
            (genera-session-position session) 0))
    (return-from genera-session-byte nil))
  (case (genera-session-prefix session)
    (:start
     (setf (genera-session-prefix session) :digits)
     (cond ((member byte '(10 141)) (return-from genera-session-byte nil))
           ((= byte 13) (setf (genera-session-prefix session) :after-cr)
                        (return-from genera-session-byte nil))))
    (:after-cr
     (setf (genera-session-prefix session) :digits)
     (when (= byte 10) (return-from genera-session-byte nil))))
  (cond ((<= 48 byte 57)
         (when (> (incf (genera-session-digits session)) +max-header-digits+)
           (framing-error "Length header exceeds digit limit"))
         (setf (genera-session-length session)
               (+ (* 10 (genera-session-length session)) (- byte 48)))
         (when (> (genera-session-length session) +max-frame-length+)
           (framing-error "Response exceeds frame limit")))
        ((and (= byte 32) (plusp (genera-session-digits session)))
         (when (zerop (genera-session-length session))
           (genera-session-error :protocol "Empty response envelope"))
         (setf (genera-session-input session)
               (make-array (genera-session-length session) :element-type '(unsigned-byte 8))))
        (t (framing-error "Expected decimal length followed by a space"))))

(defun genera-session-read-ready (session stream)
  ;; Read at most one bounded batch, yielding to queued writes between batches.
  (loop repeat 8192
        for byte = (read-byte stream nil nil)
        do (unless byte
             (genera-session-error
              (if (eq (genera-session-prefix session) :start) :transport :protocol)
              "Genera closed the connection before pending replies completed"))
           (genera-session-byte session byte)
        while (listen stream)))

(defun genera-session-hello-envelope (session)
  (append (list :version 1 :id (concatenate 'string (genera-session-nonce session) "-hello")
                :op :hello :client "lisp-mcp")
          (when (genera-session-token session) (list :token (genera-session-token session)))
          (when (genera-session-expected-world session)
            (list :expected-world (genera-session-expected-world session)))))

(defun genera-run-session (session)
  (let ((socket nil))
    (handler-case
        (unwind-protect
            (progn
              (genera-with-io session
               (setf socket (usocket:socket-connect
                            (genera-session-host session) (genera-session-port session)
                            :element-type '(unsigned-byte 8)
                            :timeout (genera-session-io-timeout session))))
              (sb-thread:with-mutex ((genera-session-lock session))
                (setf (genera-session-phase session) :hello)
                (when (genera-session-stop session)
                  (genera-session-error :closed "Genera session closed while connecting")))
              (let* ((stream (usocket:socket-stream socket))
                     (hello (genera-session-hello-envelope session))
                     (greeting
                       (genera-with-io session
                        (sb-ext:with-timeout (genera-session-io-timeout session)
                         (genera-write-envelope stream hello)
                         (let* ((size (read-frame-length stream))
                                (data (read-exactly stream
                                        (make-array size :element-type '(unsigned-byte 8))))
                                (text (map 'string #'code-char data)))
                           (genera-session-checked
                            (lambda () (mcp-v1-check-hello-response
                                        hello (mcp-v1-decode text :response)))))))))
                (unless (getf greeting :ok)
                  (genera-session-error :handshake "Genera rejected hello" greeting))
                (sb-thread:with-mutex ((genera-session-lock session))
                  (setf (genera-session-socket session) socket
                        (genera-session-hello session) greeting
                        (genera-session-cached-status session) greeting
                        (genera-session-state session) :ready
                        (genera-session-phase session) :ready)
                  (sb-thread:condition-broadcast (genera-session-changed session)))
                (loop
                  (let ((request
                          (sb-thread:with-mutex ((genera-session-lock session))
                            (when (genera-session-stop session) (return))
                            (let ((next (pop (genera-session-queue session))))
                              (when next (setf (genera-request-state next) :sending))
                              next))))
                    (when request
                      (genera-with-io session
                       (sb-ext:with-timeout (genera-session-io-timeout session)
                        (let ((payload (genera-request-payload request)))
                          (write-sequence (string-to-octets (format nil "~D " (length payload))) stream)
                          (write-sequence payload stream)
                          (force-output stream))))
                      (sb-thread:with-mutex ((genera-session-lock session))
                        (setf (genera-request-state request) :sent)))
                    (when (or (listen stream)
                              (usocket:wait-for-input socket :timeout (if request 0 .02)
                                                            :ready-only t))
                      (genera-session-read-ready session stream))))))
          (when socket
            (ignore-errors (close (usocket:socket-stream socket) :abort t))
            (ignore-errors (usocket:socket-close socket)))
          (sb-thread:with-mutex ((genera-session-lock session))
            (setf (genera-session-socket session) nil)))
      (genera-session-error (condition) (genera-session-fail session condition))
      (framing-error (condition)
        (genera-session-fail session
          (make-condition 'genera-session-error :kind :protocol :format-control "~A"
                          :format-arguments (list (princ-to-string condition)))))
      (sb-ext:timeout ()
        (genera-session-fail session
          (make-condition 'genera-session-error :kind :transport
                          :format-control "Genera socket I/O timed out")))
      (error ()
        (genera-session-fail session
          (make-condition 'genera-session-error :kind :transport
                          :format-control "Genera socket I/O failed"))))
    (unless (genera-session-failure session)
      (genera-session-fail session
        (make-condition 'genera-session-error :kind :closed
                        :format-control "Genera session closed")))))

(defun genera-close-session (session &key interrupt-io)
  "Stop the owner and close its socket; never wait for remote execution."
  (sb-thread:with-mutex ((genera-session-lock session))
    (setf (genera-session-stop session) t)
    (sb-thread:condition-broadcast (genera-session-changed session)))
  (when interrupt-io
    (ignore-errors
      (sb-thread:interrupt-thread (genera-session-owner session)
        (lambda ()
          (when (genera-session-blocking session)
            (genera-session-error :closed "Genera host I/O closed"))))))
  (sb-thread:join-thread (genera-session-owner session)
                         :timeout (+ (genera-session-io-timeout session) .5) :default nil)
  (when (sb-thread:thread-alive-p (genera-session-owner session))
    (genera-session-error :shutdown "Genera I/O owner did not stop within its I/O bound"))
  nil)

(defun genera-begin-session (&key (host *genera-host*) (port *genera-port*) token
                                expected-world (io-timeout 5))
  "Start the I/O owner without waiting for connection or hello."
  (unless (and (realp io-timeout) (> io-timeout 0))
    (error "Session I/O timeout must be positive"))
  (let ((session (%make-genera-session :host host :port port :token token
                                     :expected-world expected-world :io-timeout io-timeout)))
    (mcp-v1-encode (genera-session-hello-envelope session) :request)
    (setf (genera-session-owner session)
          (sb-thread:make-thread (lambda () (genera-run-session session)) :name "Genera I/O"))
    session))

(defun genera-open-session (&rest options)
  "Connect and authenticate once, waiting for the asynchronous owner."
  (let ((session (apply #'genera-begin-session options)))
    (let ((ready nil))
      (unwind-protect
          (progn
            (sb-thread:with-mutex ((genera-session-lock session))
              (loop while (eq (genera-session-state session) :connecting)
                    do (sb-thread:condition-wait (genera-session-changed session)
                                                 (genera-session-lock session)))
              (when (genera-session-failure session) (error (genera-session-failure session))))
            (setf ready t)
            session)
        (unless ready (genera-close-session session))))))

(defun genera-submit-request (session operation &rest fields)
  "Reserve one execution or bounded control slot; return a request handle."
  (sb-thread:with-mutex ((genera-session-lock session))
    (unless (and (eq (genera-session-state session) :ready) (not (genera-session-stop session)))
      (error (or (genera-session-failure session)
                 (make-condition 'genera-session-error :kind :closed
                                 :format-control "Genera session is closed"))))
    (unless (member operation '(:eval :status :request-status))
      (genera-session-error :unsupported "Unsupported session operation"))
    (unless (member operation (getf (genera-session-hello session) :features))
      (genera-session-error :unsupported "Listener does not advertise this operation"))
    (let* ((execution (eq operation :eval))
           (hello (genera-session-hello session))
           (envelope (append (list :version 1
                                   :id (format nil "~A-~D" (genera-session-nonce session)
                                               (incf (genera-session-counter session)))
                                   :op operation :boot-id (getf hello :boot-id)
                                   :service-id (getf hello :service-id)) fields))
           (payload (string-to-octets (mcp-v1-encode envelope :request))))
      (when (if execution (genera-session-execution session)
                (>= (genera-session-controls session) +genera-control-limit+))
        (genera-session-error :busy "Session request capacity is occupied"))
      (let ((request (%make-genera-request :envelope envelope :payload payload
                                          :execution-p execution)))
        (setf (gethash (getf envelope :id) (genera-session-pending session)) request
              (genera-session-queue session) (nconc (genera-session-queue session) (list request)))
        (if execution (setf (genera-session-execution session) request)
            (incf (genera-session-controls session)))
        (sb-thread:condition-broadcast (genera-session-changed session))
        request))))

(defun genera-await-request (session request)
  "Wait for this handle only; the socket owner can complete other controls."
  (sb-thread:with-mutex ((genera-session-lock session))
    (loop until (or (genera-request-response request) (genera-request-failure request))
          do (sb-thread:condition-wait (genera-session-changed session) (genera-session-lock session)))
    (when (genera-request-failure request) (error (genera-request-failure request)))
    (genera-request-response request)))

(defun genera-call (session operation &rest fields)
  (genera-await-request session (apply #'genera-submit-request session operation fields)))
