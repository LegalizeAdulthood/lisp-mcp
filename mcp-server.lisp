;;; -*- Mode: LISP; Syntax: Common-Lisp; Package: CL-USER -*-
;;; mcp-server.lisp - TCP eval server for Genera
;;;
;;; Protocol: length-prefixed messages over a TCP character stream.
;;; The stream transmits raw Genera char-codes on the wire.
;;; We translate to/from ASCII manually using char-to-ascii / code-char.
;;; Lengths are decimal integers (via print/read).
;;;
;;; Request:  <length><space><expression bytes>
;;; Response: <newline><length><space><result bytes>
;;;           <newline><length><space><stdout bytes>
;;;           <newline><length><space><stderr bytes>

;;; --- Length-delimited framing via print/read ---
;;; print/read work because digit char-codes match ASCII.

(defun mcp-read-length (stream)
  "Read a length using the Lisp reader. Returns NIL on EOF."
  (read stream nil nil))

(defun mcp-write-length (stream value)
  "Write a length using print (outputs newline + digits + space)."
  (print value stream))

;;; --- ASCII translation ---
;;; The TCP stream sends raw char-codes on the wire.
;;; char-to-ascii maps Genera chars to ASCII codes (returns the code
;;; unchanged for unmappable chars, so we must range-check).
;;; To send a specific ASCII byte, we use code-char to get the Genera
;;; char whose char-code equals that byte value.

(defun mcp-genera-to-wire (char)
  "Convert a Genera character to the Genera char that sends the right ASCII byte.
Returns #\\? for characters outside ASCII 9,10,13,32-126."
  (let ((ascii (char-to-ascii char)))
    (if (or (<= 32 ascii 126) (= ascii 10) (= ascii 9) (= ascii 13))
        (code-char ascii)
        #\?)))

;;; --- Reading requests ---
;;; The client sends ASCII bytes.  The stream gives us chars whose
;;; char-codes ARE the ASCII byte values (no translation on input).

(defun mcp-read-request (stream)
  "Read a length-prefixed ASCII string from STREAM. Returns NIL on EOF."
  (let ((len (mcp-read-length stream)))
    (when len
      (let ((str (make-string len)))
        (dotimes (i len)
          ;; char-code of what :tyi returns = the ASCII byte from the client
          ;; ascii-to-char converts that ASCII code to the proper Genera char
          (setf (aref str i) (ascii-to-char (char-code (send stream :tyi)))))
        str))))

;;; --- Writing responses ---

(defun mcp-write-field (stream string)
  "Write STRING as a length-prefixed field, translated to ASCII on the wire."
  (let* ((len (length string))
         (out (make-string len)))
    (dotimes (i len)
      (setf (aref out i) (mcp-genera-to-wire (aref string i))))
    (mcp-write-length stream len)
    (write-string out stream)))

;;; --- Eval ---

(defun mcp-eval (string)
  "Evaluate STRING as a Lisp expression.
Returns three values: result-string, stdout-string, stderr-string.
Redirects all interactive streams and suppresses fdefine warnings."
  (let ((stdout (make-string-output-stream))
        (stderr (make-string-output-stream))
        (query-out (make-string-output-stream)))
    (let ((result
            (condition-case (err)
                (let ((*standard-output* stdout)
                      (*error-output* stderr)
                      (*trace-output* stderr)
                      (*debug-io* (make-two-way-stream (make-string-input-stream "") query-out))
                      (*query-io* (make-two-way-stream (make-string-input-stream "") query-out))
                      (sys:inhibit-fdefine-warnings :just-warn))
                  (princ-to-string (eval (read-from-string string))))
              (error
                (format stderr "~A" err)
                ""))))
      ;; Append any query/debug output to stderr
      (let ((query-str (get-output-stream-string query-out)))
        (when (plusp (length query-str))
          (format stderr "~A" query-str)))
      (values result
              (get-output-stream-string stdout)
              (get-output-stream-string stderr)))))

;;; --- Server ---

(defun mcp-handle-client (stream)
  "Service loop: read eval requests, send back results."
  (unwind-protect
      (loop for request = (mcp-read-request stream)
            while request
            do (multiple-value-bind (result stdout stderr)
                   (mcp-eval request)
                 (mcp-write-field stream result)
                 (mcp-write-field stream stdout)
                 (mcp-write-field stream stderr)
                 (send stream :force-output)))
    (close stream :abort t)))

(net:define-server :mcp-server (:medium :byte-stream :stream stream)
  (mcp-handle-client stream))

(tcp:add-tcp-port-for-protocol :mcp-server 8888)
