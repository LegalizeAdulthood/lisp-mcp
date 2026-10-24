;;; Portable framing checks; the Genera stream/character APIs are fixtures.
;;; Actual Genera compatibility requires framing.py --live-genera.

(load (merge-pathnames "../lisp-mcp.lisp" *load-truename*))

(defpackage #:genera-framing-fixture (:use #:cl))
(in-package #:genera-framing-fixture)

(defun send (stream operation)
  (ecase operation (:tyi (read-char stream nil nil))))
(defun ascii-to-char (code) (code-char code))
(defun char-to-ascii (char) (char-code char))

;; Load the actual portable framing definitions, stopping before Genera eval.
(with-open-file (source (merge-pathnames "../eval-server.lisp" *load-truename*))
  (loop for form = (read source nil nil)
        while (and form (not (eq (first form) 'defvar)))
        do (eval form)))

(defpackage #:framing-tests (:use #:cl))
(in-package #:framing-tests)

(defvar *checks* 0)

(defun check (expected actual)
  (incf *checks*)
  (unless (equalp expected actual)
    (error "Expected ~S, got ~S" expected actual)))

(defun rejects (function &optional (type 'error))
  (check t (handler-case (progn (funcall function) nil)
             (error (e) (typep e type)))))

;; Force READ-SEQUENCE to return short chunks even before EOF.
(defclass chunk-input (sb-gray:fundamental-binary-input-stream)
  ((data :initarg :data :reader data)
   (chunk :initarg :chunk :reader chunk)
   (position :initform 0 :accessor offset)))

(defmethod stream-element-type ((stream chunk-input)) '(unsigned-byte 8))
(defmethod sb-gray:stream-read-byte ((stream chunk-input))
  (if (= (offset stream) (length (data stream)))
      :eof
      (prog1 (aref (data stream) (offset stream)) (incf (offset stream)))))
(defmethod sb-gray:stream-read-sequence
    ((stream chunk-input) sequence &optional (start 0) end)
  (let ((stop (min (or end (length sequence))
                   (+ start (chunk stream))
                   (+ start (- (length (data stream)) (offset stream))))))
    (loop for i from start below stop
          do (setf (elt sequence i) (read-byte stream)))
    stop))

(defun binary-input (string &optional (chunk 2))
  (make-instance 'chunk-input :chunk chunk
                 :data (map '(vector (unsigned-byte 8)) #'char-code string)))

(defun host-field (string &optional (chunk 2))
  (lisp-mcp::read-field (binary-input string chunk)))
(defun listener-request (string)
  (genera-framing-fixture::mcp-read-request (make-string-input-stream string)))

(dolist (prefix (list "" (string #\Linefeed) (string #\Return)
                     (coerce '(#\Return #\Linefeed) 'string)))
  (dolist (payload '("" "abc"))
    (let ((frame (format nil "~A~D ~A" prefix (length payload) payload)))
      (check payload (host-field frame))
      (check payload (listener-request frame)))))

(let ((host (binary-input "1 a1 b"))
      (listener (make-string-input-stream "1 a1 b")))
  (check "a" (lisp-mcp::read-field host))
  (check "b" (lisp-mcp::read-field host))
  (rejects (lambda () (lisp-mcp::read-field host)) 'lisp-mcp::framing-error)
  (check "a" (genera-framing-fixture::mcp-read-request listener))
  (check "b" (genera-framing-fixture::mcp-read-request listener))
  (check nil (genera-framing-fixture::mcp-read-request listener)))

(dolist (frame (list "1" "1 " "3 ab" "+1 a" "-1 a" " 1 a" "1:a"
                    "1.0 a" "junk1 a" "#.(error) " "1048577 "
                    "00000000000 " (format nil "~%")
                    (format nil "~%~%1 a") (format nil "1~%a")
                    (format nil "~C1 a" #\Tab)))
  (rejects (lambda () (host-field frame)) 'lisp-mcp::framing-error)
  (rejects (lambda () (listener-request frame))))
(check "" (host-field "0000000000 "))
(check "a" (host-field (format nil "~C1 a" (code-char 141))))
(rejects (lambda () (host-field (format nil "~C~C1 a" (code-char 141) (code-char 141))))
         'lisp-mcp::framing-error)
(check "" (listener-request "0000000000 "))
(check nil (listener-request ""))
(rejects (lambda () (host-field "")) 'lisp-mcp::framing-error)

(let ((*read-base* 16) (*read-eval* t))
  (check "0123456789" (host-field "10 0123456789"))
  (check "0123456789" (listener-request "10 0123456789")))

(dolist (code '(0 1 8 11 12 14 31 127 128 255 256))
  (rejects (lambda () (lisp-mcp::string-to-octets (string (code-char code))))
           'lisp-mcp::framing-error)
  (rejects (lambda () (listener-request (format nil "1 ~C" (code-char code))))))

(let* ((text (coerce (append '(#\Tab #\Linefeed #\Return)
                             (loop for code from 32 to 126 collect (code-char code)))
                     'string))
       (bytes (lisp-mcp::string-to-octets text)))
  (check (map 'vector #'char-code text) bytes)
  (check text (listener-request (format nil "~D ~A" (length text) text))))
(check "?" (host-field (format nil "1 ~C" (code-char 255))))

(let* ((text (make-string lisp-mcp::+max-frame-length+ :initial-element #\a))
       (frame (format nil "~D ~A" (length text) text)))
  (check text (host-field frame 65536))
  (check text (listener-request frame))
  (check (length text) (length (lisp-mcp::string-to-octets text)))
  (let ((oversized (concatenate 'string text "a")))
    (rejects (lambda () (lisp-mcp::string-to-octets oversized))
             'lisp-mcp::framing-error)
    (rejects (lambda ()
               (genera-framing-fixture::mcp-write-field
                (make-string-output-stream) oversized)))))

(let ((*print-base* 16) (*print-radix* t))
  (check (format nil "~%10 abcdefghij")
         (with-output-to-string (stream)
           (genera-framing-fixture::mcp-write-field stream "abcdefghij"))))
(dolist (length '(-1 1048577 1.5 nil))
  (rejects (lambda () (genera-framing-fixture::mcp-write-length
                      (make-string-output-stream) length))))

(format t "~&Framing unit checks: ~D passed.~%" *checks*)
