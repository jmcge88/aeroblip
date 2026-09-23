#pragma once

#include <tiny_websockets/internals/ws_common.hpp>
#include <tiny_websockets/internals/data_frame.hpp>

namespace websockets {
    enum class MessageType {
        Empty,
        Text, Binary,
        Ping, Pong, Close
    };

    MessageType messageTypeFromOpcode(uint8_t opcode);

    enum class MessageRole {
        Complete, First, Continuation, Last 
    };

    // The class the user will interact with as a message
    // This message can be partial (so practically this is a Frame and not a message)
    // aeroblip patch (vendored from gilmaimon/ArduinoWebsockets 0.5.4):
    // upstream stores the payload in a `const WSString`, so the constructor
    // copies it and the implicit move constructor copies it again - three
    // resident copies of every frame by the time the user callback runs. On a
    // board without PSRAM a 44 KB frame therefore needs ~130 KB of heap and
    // throws bad_alloc. Take rvalue payloads by move and drop the const so
    // the compiler-generated move constructor really moves: one copy total.
    struct WebsocketsMessage {
        WebsocketsMessage(MessageType msgType, const WSString& msgData, MessageRole msgRole = MessageRole::Complete) : _type(msgType), _length(msgData.size()), _data(msgData), _role(msgRole) {}
        WebsocketsMessage(MessageType msgType, WSString&& msgData, MessageRole msgRole = MessageRole::Complete) : _type(msgType), _length(msgData.size()), _data(std::move(msgData)), _role(msgRole) {}
        WebsocketsMessage() : WebsocketsMessage(MessageType::Empty, "", MessageRole::Complete) {}

        static WebsocketsMessage CreateFromFrame(internals::WebsocketsFrame frame, MessageType overrideType = MessageType::Empty) {
            auto type = overrideType;
            if(type == MessageType::Empty) {
                type = messageTypeFromOpcode(frame.opcode);
            }

            // deduce role
            MessageRole msgRole = MessageRole::Complete;
            if(frame.isNormalUnfragmentedMessage()) {
                msgRole = MessageRole::Complete;
            } else if(frame.isBeginningOfFragmentsStream()) {
                msgRole = MessageRole::First;
            } else if(frame.isContinuesFragment()) {
                msgRole = MessageRole::Continuation;
            } else if(frame.isEndOfFragmentsStream()) {
                msgRole = MessageRole::Last;
            }

            return WebsocketsMessage(type, std::move(frame.payload), msgRole);
        }
        
        // for validation
        bool isEmpty() const { return this->_type == MessageType::Empty; }

        // Type Helper Functions
        MessageType type() const { return this->_type; }

        bool isText() const { return this->_type == MessageType::Text; }
        bool isBinary() const { return this->_type == MessageType::Binary; }
        
        bool isPing() const { return this->_type == MessageType::Ping; }
        bool isPong() const { return this->_type == MessageType::Pong; }
        
        bool isClose() const { return this->_type == MessageType::Close; }

        
        // Role Helper Function
        MessageRole role() const { return this->_role; }

        bool isComplete() const { return this->_role == MessageRole::Complete; }
        bool isPartial() const { return this->_role != MessageRole::Complete; }
        bool isFirst() const { return this->_role == MessageRole::First; }
        bool isContinuation() const { return this->_role == MessageRole::Continuation; }
        bool isLast() const { return this->_role == MessageRole::Last; }


        WSInterfaceString data() const { return internals::fromInternalString(this->_data); }
        const WSString& rawData() const { return this->_data; }
        const char* c_str() const { return this->_data.c_str(); }

        uint32_t length() const { return this->_length; }

        class StreamBuilder {
        public:
            StreamBuilder(bool dummyMode = false) : _dummyMode(dummyMode), _empty(true) {}

            void first(const internals::WebsocketsFrame& frame) {
                if(this->_empty == false) {
                    badFragment();
                    return;
                }

                this->_empty = false;
                if(frame.isBeginningOfFragmentsStream()) {
                    this->_isComplete = false;
                    this->_didErrored = false;

                    if(this->_dummyMode == false) {
                        this->_content = std::move(frame.payload);
                    }

                    this->_type = messageTypeFromOpcode(frame.opcode);
                    if(this->_type == MessageType::Empty) {
                        badFragment();
                    }
                } else {
                    this->_didErrored = true;
                }
            }

            void append(const internals::WebsocketsFrame& frame) {
                if(isErrored()) return;
                if(isEmpty() || isComplete()) {
                    badFragment();
                    return;
                }

                if(frame.isContinuesFragment()) {
                    if(this->_dummyMode == false) {
                        this->_content += std::move(frame.payload);
                    }
                } else {
                    badFragment();
                }
            }

            void end(const internals::WebsocketsFrame& frame) {
                if(isErrored()) return;
                if(isEmpty() || isComplete()) {
                    badFragment();
                    return;
                }

                if(frame.isEndOfFragmentsStream()) {
                    if(this->_dummyMode == false) {
                        this->_content += std::move(frame.payload);
                    }
                    this->_isComplete = true;
                } else {
                    badFragment();
                }
            }

            void badFragment() {
                this->_didErrored = true;
                this->_isComplete = false;
            }

            bool isErrored() {
                return this->_didErrored;
            }

            bool isOk() {
                return !this->_didErrored;
            }

            bool isComplete() {
                return this->_isComplete;
            }

            bool isEmpty() {
                return this->_empty;
            }
            
            MessageType type() {
                return this->_type;
            }

            WebsocketsMessage build() {
                return WebsocketsMessage(
                    this->_type, 
                    std::move(this->_content),
                    MessageRole::Complete
                );
            }

        private:
            bool _dummyMode;
            bool _empty;
            bool _isComplete = false;
            WSString _content;
            MessageType _type;
            bool _didErrored;
        };

    private:
        const MessageType _type;
        const uint32_t _length;
        WSString _data; // not const: see the patch note above (must stay movable)
        const MessageRole _role;
    };
}